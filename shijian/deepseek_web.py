"""DeepSeek Search/Fetch JSON contracts, bounded responses and per-request accounting."""

import asyncio
import json
import logging
import time
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from shijian.web import FetchError, canonical_url, public_addresses

log = logging.getLogger("shijian.web_usage")


class WebAPIError(FetchError):
    def __init__(self, kind, status):
        self.status_code = status
        super().__init__(f"DeepSeek {kind} 返回 HTTP {status}；未取得可用资料")


class SearchRow(BaseModel):
    title: str
    url: HttpUrl
    snippet: str
    summary: str
    content: str
    authority: int = Field(default=-1, ge=-1, le=4)
    rerank_score: float = Field(default=0, ge=0, le=3)


class SearchReply(BaseModel):
    results: list[SearchRow]


class PageReply(BaseModel):
    title: str
    content: str = Field(max_length=500_000)


async def request(kind, body, config, on_usage=None):
    if error := config.web_error():
        raise FetchError(error)
    # Tools have a separate configurable endpoint; never infer it from a chat relay URL.
    base = canonical_url(config.deepseek_tools_base_url).rstrip("/")
    if urlsplit(base).query:
        raise FetchError("DEEPSEEK_TOOLS_BASE_URL不能包含查询参数")
    status, billable, outcome = None, None, "failed"
    started = time.monotonic()
    try:
        async with asyncio.timeout(config.request_timeout):
            async with httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    base + "/v1/tools/" + kind,
                    headers={"Authorization": "Bearer " + config.tools_key()},
                    json=body,
                ) as response:
                    status = response.status_code
                    if status != 200:
                        billable = False
                        raise WebAPIError(kind, status)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        raw.extend(chunk)
                        if len(raw) > config.fetch_max_bytes:
                            raise FetchError("DeepSeek工具响应超过体积上限")
                    try:
                        payload = json.loads(raw)
                        parsed = (SearchReply if kind == "search" else PageReply).model_validate(payload)
                    except (ValueError, UnicodeError, ValidationError) as exc:
                        raise FetchError("DeepSeek工具响应不符合JSON协议") from exc
                    billable = bool(parsed.results) if kind == "search" else bool(parsed.content.strip())
                    outcome = "returned" if billable else "empty"
                    return parsed
    finally:
        price = config.search_price_cny_per_1000 if kind == "search" else config.fetch_price_cny_per_1000
        record = {
            "provider": "deepseek",
            "kind": kind,
            "http_status": status,
            "outcome": outcome,
            "billable": billable,
            "estimated_cost_cny": str(price / Decimal(1000) * int(billable))
            if price is not None and billable is not None
            else None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        log.info("external_usage %s", json.dumps(record, ensure_ascii=False))
        if on_usage is not None:
            await on_usage(record)


async def search(query, config, on_usage=None):
    query = query.strip()
    if not 1 <= len(query) <= 128:
        raise FetchError("DeepSeek搜索词必须为1至128字符；请拆分或改写")
    reply = await request("search", {"query": query, "count": 5, "need_content": False}, config, on_usage)
    # Summaries are only leads, never registered as read evidence.
    return [
        {
            "title": row.title[:250],
            "url": canonical_url(str(row.url)),
            "snippet": row.snippet[:600],
            "authority": row.authority,
            "rerank_score": row.rerank_score,
        }
        for row in reply.results[:5]
    ]


async def fetch(url, config, on_usage=None):
    url = canonical_url(url)
    p = urlsplit(url)
    # The provider performs the downstream fetch. Reject private destinations before handing it a URL.
    async with asyncio.timeout(config.request_timeout):
        await asyncio.to_thread(public_addresses, p.hostname, p.port or (443 if p.scheme == "https" else 80))
    reply = await request("fetch", {"url": url}, config, on_usage)
    if len(reply.content.strip()) < 20:
        raise FetchError("未取得足够网页正文，不能把摘要或空页面当作证据")
    if "Request unsuccessful" in reply.content and "Incapsula incident ID" in reply.content:
        raise FetchError("Fetch返回站点访问拦截页，不能作为史料正文；该次200响应仍按服务商规则记录用量")
    return {
        "url": url,
        "title": reply.title[:250] or p.hostname,
        "text": reply.content,
        "edition": "DeepSeek Fetch提取的Markdown快照；页面版本未独立核对",
        "provenance_note": "正文由DeepSeek Fetch提取，位置基于保存的Markdown；未取得原始HTML或重定向终点，来源及史实仍需核查。",
    }
