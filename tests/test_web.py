import socket

import httpx
import pytest

from shijian.config import Settings
from shijian.web import FetchError, canonical_url, public_addresses, search_web


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org",
        "https://user:password@example.org",
        "https://example.org:22/",
    ],
)
def test_forbidden_urls(url):
    with pytest.raises(FetchError):
        canonical_url(url)


def test_canonicalization():
    assert (
        canonical_url("HTTPS://Example.org:443/a?utm_source=x&b=2&a=1#s") == "https://example.org/a?a=1&b=2"
    )
    assert canonical_url("https://example.org/A") != canonical_url("https://example.org/a")


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.1.0.2", "169.254.169.254", "::1", "::ffff:127.0.0.1", "192.168.1.1", "0.0.0.0"]
)
def test_ssrf_rejected_after_dns(monkeypatch, ip):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
    )
    with pytest.raises(FetchError):
        public_addresses("malicious.example", 443)


async def test_missing_search_key_is_explicit():
    with pytest.raises(FetchError, match="SEARCH_API_KEY"):
        await search_web("史思明", Settings(search_provider="tavily", search_api_key="", _env_file=None))


async def test_search_provider_contract_and_normalization(monkeypatch):
    from shijian import web

    client_class = httpx.AsyncClient

    def handler(request):
        assert request.headers["authorization"] == "Bearer TEST-ONLY"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "TEST",
                        "url": "https://example.org/a?utm_source=x#s",
                        "content": "TEST-SNIPPET",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    hits = await search_web(
        "史思明", Settings(search_provider="tavily", search_api_key="TEST-ONLY", _env_file=None)
    )
    assert hits == [{"title": "TEST", "url": "https://example.org/a", "snippet": "TEST-SNIPPET"}]


async def test_search_provider_bad_payload_not_fake_success(monkeypatch):
    from pydantic import ValidationError

    from shijian import web

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"unexpected": "no results contract"})
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ValidationError):
        await search_web(
            "史思明", Settings(search_provider="tavily", search_api_key="TEST-ONLY", _env_file=None)
        )
