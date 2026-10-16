"""Bounded HTTP fetch with validated, pinned DNS addresses, including every redirect."""

import asyncio
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, HttpUrl


class FetchError(ValueError):
    pass


class SearchHit(BaseModel):
    title: str
    url: HttpUrl
    content: str = ""


class SearchResponse(BaseModel):
    results: list[SearchHit]


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise FetchError("只允许不含用户凭据的 HTTP/HTTPS URL")
    host = p.hostname.encode("idna").decode().lower()
    try:
        port = p.port
    except ValueError as exc:
        raise FetchError("URL 端口无效") from exc
    if port not in (None, 80, 443):
        raise FetchError("网页抓取仅允许 80/443 端口")
    if ":" in host:
        host = f"[{host}]"
    if port and port != (443 if p.scheme.lower() == "https" else 80):
        host += f":{port}"
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((p.scheme.lower(), host, p.path or "/", urlencode(sorted(query)), ""))


def public_addresses(host: str, port: int):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise FetchError("禁止访问本机、私有、保留或链路本地网络地址")
    return addresses


def fetch_sync(url: str, timeout: float, max_bytes: int) -> dict:
    deadline = time.monotonic() + timeout
    for _ in range(5):
        url = canonical_url(url)
        p = urlsplit(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        addresses = public_addresses(p.hostname, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("网页读取超时")
        family, socktype, proto, _, sockaddr = addresses[0]
        sock = socket.socket(family, socktype, proto)
        conn = http.client.HTTPConnection(p.hostname, port, timeout=remaining)
        try:
            sock.settimeout(remaining)
            sock.connect(sockaddr)  # DNS cannot change between verification and connection.
            if p.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
            conn.sock = sock
            path = urlunsplit(("", "", p.path or "/", p.query, ""))
            # Unicode paths must be percent-encoded by callers (HttpUrl does so).
            from urllib.parse import quote

            path = quote(path, safe="/%?=&:+,;@!$'()*~-._")
            conn.request(
                "GET",
                path,
                headers={
                    "Host": p.netloc,
                    "User-Agent": "ShijianResearch/0.1",
                    "Accept-Encoding": "identity",
                    "Accept": "text/html,text/plain",
                },
            )
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise FetchError("重定向缺少 Location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise FetchError(f"网页 HTTP 状态 {response.status}")
            content_type = response.getheader("Content-Type", "")
            if not any(t in content_type.lower() for t in ("text/html", "text/plain", "application/xhtml")):
                raise FetchError("仅支持 HTML 或纯文本网页，不处理 PDF、图片及二进制响应")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise FetchError("拒绝压缩响应，避免解压体积绕过限制")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("网页读取超时")
                sock.settimeout(remaining)
                chunk = response.read(min(16384, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError("网页超过大小限制")
            body = b"".join(chunks)
        finally:
            conn.close()
            sock.close()
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True)[:250] if soup.title else p.hostname
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        main = max(soup.select(".mw-parser-output"), key=lambda x: len(x.get_text()), default=None)
        main = main or soup.find("article") or soup.find("main") or soup
        text = main.get_text("\n", strip=True)
        if len(text) < 20:
            raise FetchError("未取得足够正文；可能是登录页或脚本渲染页面")
        return {"url": url, "title": title, "text": text}
    raise FetchError("重定向次数过多")


async def fetch_page(url, config):
    # DNS resolution is also bounded for the caller. The thread has no persistence side effects.
    async with asyncio.timeout(config.request_timeout):
        return await asyncio.to_thread(fetch_sync, url, config.request_timeout, config.fetch_max_bytes)


async def search_web(query: str, config) -> list[dict]:
    if not config.search_api_key.get_secret_value():
        raise FetchError("联网模式缺少 SEARCH_API_KEY；搜索服务独立于 DeepSeek")
    async with httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=False) as client:
        response = await client.post(
            config.search_api_url,
            headers={"Authorization": "Bearer " + config.search_api_key.get_secret_value()},
            json={"query": query, "max_results": 5, "include_raw_content": False},
        )
        response.raise_for_status()
        if len(response.content) > config.fetch_max_bytes:
            raise FetchError("搜索服务响应过大")
        payload = SearchResponse.model_validate(response.json())
    return [
        {
            "title": row.title[:250],
            "url": canonical_url(str(row.url)),
            "snippet": row.content[:600],
        }
        for row in payload.results[:5]
    ]
