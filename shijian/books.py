"""Read a specific Wikisource chapter/revision, never crawl a complete library."""

from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError

from shijian.web import FetchError, canonical_url, extract_html, fetch_json

BASE = "https://zh.wikisource.org"
API = BASE + "/w/rest.php/v1"


class BookHit(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    title: str = Field(max_length=500)
    excerpt: str = ""


class BookHits(BaseModel):
    pages: list[BookHit]


async def search_books(query, config):
    payload = await fetch_json(API + "/search/page?" + urlencode({"q": query, "limit": 5}), config)
    try:
        rows = BookHits.model_validate(payload).pages
    except ValidationError as exc:
        raise FetchError("维基文库检索响应格式无效") from exc
    return [
        {
            "title": r.title[:250],
            "url": BASE + "/wiki/" + quote(r.key, safe=""),
            "snippet": BeautifulSoup(r.excerpt, "html.parser").get_text()[:400],
        }
        for r in rows[:5]
    ]


async def read_wikisource(url, config):
    p = urlsplit(canonical_url(url))
    if p.hostname != "zh.wikisource.org":
        raise FetchError("此文献适配器只允许中文维基文库")
    query = parse_qs(p.query)
    title = unquote(p.path[6:]) if p.path.startswith("/wiki/") else query.get("title", [""])[0]
    revision = query.get("oldid", [None])[0]
    if revision is not None and not revision.isdecimal():
        raise FetchError("文献修订号无效")
    if revision:
        data = await fetch_json(API + "/revision/" + revision + "/with_html", config)
        actual_title = (
            data.get("page", {}).get("title")
            if isinstance(data, dict) and isinstance(data.get("page"), dict)
            else None
        )
        revision = data.get("id") if isinstance(data, dict) else None
    else:
        if not title or len(title) > 500:
            raise FetchError("需要维基文库卷章页面地址")
        data = await fetch_json(API + "/page/" + quote(title, safe="") + "/with_html", config)
        actual_title = data.get("title") if isinstance(data, dict) else None
        revision = (
            data.get("latest", {}).get("id")
            if isinstance(data, dict) and isinstance(data.get("latest"), dict)
            else None
        )
    if (
        not isinstance(actual_title, str)
        or not isinstance(revision, int)
        or revision <= 0
        or not isinstance(data.get("html"), str)
    ):
        raise FetchError("未取得卷章标题、修订号或正文，未保存不完整快照")
    fixed = BASE + "/w/index.php?" + urlencode({"title": actual_title, "oldid": revision})
    page = extract_html(data["html"].encode(), fixed)
    license_data = data.get("license")
    license_name = license_data.get("title") if isinstance(license_data, dict) else None
    if not isinstance(license_name, str):
        license_name = "许可未在 API 响应中提供，请核对来源站点"
    return {
        **page,
        "title": actual_title[:250],
        "revision": revision,
        "edition": "维基文库数字文本，修订 " + str(revision),
        "provenance_note": "按需取得的单页快照；"
        + license_name
        + "。保留固定修订链接；文本未独立校勘，不自动视为事件当时的一手记录。",
    }
