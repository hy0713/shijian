import io
import socket

import pytest

from shijian import web


def install_transport(monkeypatch, responses):
    connections, pins = [], []

    class Sock:
        def __init__(self, *a):
            pass

        def settimeout(self, timeout):
            assert timeout > 0

        def connect(self, address):
            pins.append(address)

        def close(self):
            pass

    class Response:
        def __init__(self, code, headers, body):
            self.status, self.headers, self.stream = code, headers, io.BytesIO(body)

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def read(self, size):
            return self.stream.read(size)

    class Connection:
        def __init__(self, host, port, timeout):
            self.host = host

        def request(self, method, path, headers):
            connections.append((self.host, path))

        def getresponse(self):
            return Response(*responses.pop(0))

        def close(self):
            pass

    def resolve(host, port, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1" if host == "internal.test" else "93.184.216.34", port),
            )
        ]

    monkeypatch.setattr(web.socket, "socket", Sock)
    monkeypatch.setattr(web.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(web.http.client, "HTTPConnection", Connection)
    return connections, pins


def test_redirect_to_private_host_is_checked_again(monkeypatch):
    connections, pins = install_transport(
        monkeypatch, [(302, {"Location": "http://internal.test/secret"}, b"")]
    )
    with pytest.raises(web.FetchError, match="禁止"):
        web.fetch_sync("http://public.test", 5, 1000)
    assert len(connections) == 1 and pins == [("93.184.216.34", 80)]


def test_fetch_pins_dns_and_reads_largest_wiki_body(monkeypatch):
    raw = '<html><title>原文标题</title><div class="mw-parser-output"></div><div class="mw-parser-output"><p>这是一段足够长的真实响应协议测试文字，仅用作测试，不是古籍。</p></div></html>'.encode()
    connections, pins = install_transport(
        monkeypatch, [(200, {"Content-Type": "text/html; charset=utf-8"}, raw)]
    )
    result = web.fetch_sync("http://public.test", 5, 1000)
    assert "原文标题" == result["title"] and "协议测试" in result["text"]
    assert pins == [("93.184.216.34", 80)]


@pytest.mark.parametrize(
    "headers,body,match",
    [
        ({"Content-Type": "text/html"}, b"x" * 100, "大小"),
        ({"Content-Type": "application/octet-stream"}, b"x", "仅支持"),
        ({"Content-Type": "text/html", "Content-Encoding": "gzip"}, b"x", "压缩"),
    ],
)
def test_fetch_limits(monkeypatch, headers, body, match):
    install_transport(monkeypatch, [(200, headers, body)])
    with pytest.raises(web.FetchError, match=match):
        web.fetch_sync("http://public.test", 5, 50)
