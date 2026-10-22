"""Online-mode and DeepSeek tool protocol tests. All external services are test doubles."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from shijian import deepseek_web as web
from shijian.agent import ResearchAgent
from shijian.config import Settings
from shijian.schema import DocumentInput, ResearchInput
from shijian.web import FetchError
from tests.fakes import ScriptedModel


def config(**overrides):
    return Settings(
        _env_file=None,
        **{
            "search_provider": "deepseek",
            "deepseek_tools_api_key": "TEST-ONLY",
            "deepseek_tools_base_url": "https://api.deepseek.com",
            "search_price_cny_per_1000": "5",
            "fetch_price_cny_per_1000": "5",
            **overrides,
        },
    )


def mock_http(monkeypatch, handler):
    base = httpx.AsyncClient

    class MockClient(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", MockClient)
    monkeypatch.setattr(web, "public_addresses", lambda *args: [])


async def test_search_fetch_contract_and_metering(monkeypatch):
    calls, usage = [], []

    def handler(request):
        assert request.headers["authorization"] == "Bearer TEST-ONLY"
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "TEST source",
                        "url": "https://example.org/page?utm_source=x#s",
                        "snippet": "TEST lead only",
                        "summary": "TEST not read evidence",
                        "content": "",
                    }
                ]
            }
            if request.url.path.endswith("search")
            else {
                "title": "TEST source",
                "content": "# TEST ONLY\n\nThis is a protocol fixture, not historical evidence.",
            },
        )

    async def report(row):
        usage.append(row)

    mock_http(monkeypatch, handler)
    hits = await web.search("TEST query", config(), report)
    assert hits[0]["url"] == "https://example.org/page"
    assert "text" not in hits[0] and "content" not in hits[0]
    page = await web.fetch(hits[0]["url"], config(), report)
    assert page["text"].startswith("# TEST ONLY")
    assert calls == [
        ("/v1/tools/search", {"query": "TEST query", "count": 5, "need_content": False}),
        ("/v1/tools/fetch", {"url": "https://example.org/page"}),
    ]
    assert [r["estimated_cost_cny"] for r in usage] == ["0.005", "0.005"]
    assert all(r["billable"] for r in usage)


@pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 500])
async def test_http_errors_do_not_retry_or_expose_remote_body(monkeypatch, caplog, status):
    calls, usage = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "TEST_SECRET_PROVIDER_BODY"})

    async def report(row):
        usage.append(row)

    mock_http(monkeypatch, handler)
    with caplog.at_level("INFO", logger="shijian.web_usage"):
        with pytest.raises(web.WebAPIError) as raised:
            await web.search("TEST query", config(), report)
    assert raised.value.status_code == status and len(calls) == 1
    assert usage[0]["billable"] is False and usage[0]["estimated_cost_cny"] == "0.000"
    assert "TEST_SECRET" not in caplog.text + str(raised.value) + str(usage)


@pytest.mark.parametrize("kind", ["search", "fetch"])
async def test_empty_responses_have_no_invented_content_or_charge(monkeypatch, kind):
    usage = []
    mock_http(
        monkeypatch,
        lambda _: httpx.Response(
            200, json={"results": []} if kind == "search" else {"title": "", "content": ""}
        ),
    )

    async def report(row):
        usage.append(row)

    if kind == "search":
        assert await web.search("TEST", config(), report) == []
    else:
        with pytest.raises(FetchError, match="正文"):
            await web.fetch("https://example.org/", config(), report)
    assert usage[0]["outcome"] == "empty" and usage[0]["billable"] is False


@pytest.mark.parametrize("fault", ["malformed", "oversized", "timeout"])
async def test_unknown_usage_not_reported_as_free(monkeypatch, fault):
    usage = []

    def handler(request):
        if fault == "timeout":
            raise httpx.ReadTimeout("TEST private SDK body")
        return httpx.Response(200, content=b"invalid JSON" if fault == "malformed" else b"x" * 2048)

    async def report(row):
        usage.append(row)

    mock_http(monkeypatch, handler)
    with pytest.raises((FetchError, httpx.ReadTimeout)):
        await web.search("TEST", config(fetch_max_bytes=1024), report)
    assert usage[0]["billable"] is None and usage[0]["estimated_cost_cny"] is None


async def test_invalid_query_and_private_url_never_reach_provider(monkeypatch):
    def forbidden(*args):
        pytest.fail("provider must not be called")

    monkeypatch.setattr(web, "request", forbidden)
    for query in (" ", "x" * 129):
        with pytest.raises(FetchError):
            await web.search(query, config())
    for url in (
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "https://user:secret@example.org",
    ):
        with pytest.raises(FetchError):
            await web.fetch(url, config())


@pytest.mark.parametrize("duplicate", [False, True])
async def test_online_reads_only_just_fetched_page_and_preserves_origin(
    monkeypatch, corpus, demo_docs, duplicate
):
    from shijian import agent as agent_module

    original_id = corpus.import_document(demo_docs[0])["source_id"]
    body = (
        "\n".join(["TEST irrelevant opening paragraph. " * 15] * 7)
        + "\nTARGET_TOKEN is in the relevant tail paragraph. TEST ONLY."
    )
    if duplicate:
        corpus.import_document(
            DocumentInput(title="TEST old mirror", raw_text=body, source_url="https://example.org/old"),
            index=False,
        )
    corpus.vectors.fail = True  # Online reading must not wait for embedding or vector availability.
    events, remembered = [], []

    async def emit(kind, row):
        events.append((kind, row))

    async def nop():
        pass

    async def fake_search(query, cfg, on_usage):
        await on_usage(
            {"kind": "search", "provider": "deepseek", "billable": True, "estimated_cost_cny": "0.005"}
        )
        return [{"title": "TEST current page", "url": "https://example.org/world", "snippet": "TEST lead"}]

    async def fake_fetch(url, cfg, on_usage):
        await on_usage(
            {"kind": "fetch", "provider": "deepseek", "billable": True, "estimated_cost_cny": "0.005"}
        )
        return {"title": "TEST current page", "url": url, "text": body}

    class OnlineModel(ScriptedModel):
        async def choose(self, messages, tools):
            assert "search_corpus" not in [t.name for t in tools]
            self.calls += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_web" if self.calls == 1 else "fetch_page",
                        "args": {"query": "TEST"}
                        if self.calls == 1
                        else {"url": "https://example.org/world", "focus": "TARGET_TOKEN"},
                        "id": str(self.calls),
                    }
                ],
            )

    async def remember(e):
        remembered.append(e)

    monkeypatch.setattr(agent_module, "search_web", fake_search)
    monkeypatch.setattr(web, "fetch", fake_fetch)
    agent = ResearchAgent(config(max_web_searches=1), OnlineModel(), corpus, emit, nop, remember)
    result = await agent.run("TEST cross-topic question", "online")
    assert agent.search_count == agent.page_count == 1
    assert result["metrics"]["web_usage"]["estimated_tool_cost_cny"] == "0.010"
    assert any("TARGET_TOKEN" in e["text"] for e in remembered)
    assert all(
        e["source_id"] != original_id and e["url"] == "https://example.org/world" and e["retrieved_at"]
        for e in remembered
    )
    assert all(r["url"] == "https://example.org/world" for r in result["citations"])
    assert [k for k, d in events if k == "external_usage"] == ["external_usage", "external_usage"]
    with pytest.raises(FetchError, match="不允许"):
        await agent.tools["search_corpus"].ainvoke({"query": "史思明"})
    # Further page reading retains this task's actual origin even after corpus-body deduplication.
    other = next(pid for pid in agent.discovered_passages if pid not in agent.evidence)
    await agent.tools["read_passage"].ainvoke({"passage_id": other})
    assert all(e["url"] == "https://example.org/world" for e in remembered)
    await agent.execute(
        {
            "messages": [],
            "tool_calls": 2,
            "rounds": 0,
            "limits": [],
            "selected": [
                {"name": "search_web", "args": {"query": "TEST"}, "id": "cached"},
            ],
        }
    )
    assert agent.search_count == 1 and len(agent.external_requests) == 2
    assert any(kind == "cache_hit" for kind, _ in events)
    with pytest.raises(FetchError, match="搜索次数上限"):
        await agent.tools["search_web"].ainvoke({"query": "TEST other query"})
    with pytest.raises(FetchError, match="仅允许"):
        await agent.tools["fetch_page"].ainvoke({"url": "https://example.org/never-searched"})
    agent.external_requests.append({"estimated_cost_cny": None, "billable": None})
    assert agent.web_usage()["known_tool_cost_cny"] == "0.010"
    assert agent.web_usage()["estimated_tool_cost_cny"] is None
    assert agent.web_usage()["unknown_billing_requests"] == 1


def test_default_mode_and_optional_tool_key():
    assert ResearchInput(question="TEST QUESTION").mode == "online"
    cfg = config(deepseek_tools_api_key="", deepseek_api_key="TEST-CHAT-KEY")
    assert cfg.tools_key() == "TEST-CHAT-KEY" and cfg.web_ready()


async def test_planner_keeps_unread_leads_and_failed_fetches(monkeypatch, corpus):
    from shijian import agent as agent_module

    async def nop(*args):
        pass

    async def search(query, cfg, observer):
        return [
            {"url": "https://example.org/failed", "title": "TEST first", "snippet": "TEST"},
            {"url": "https://example.org/alternative", "title": "TEST second", "snippet": "TEST"},
        ]

    async def fetch(*args):
        raise web.WebAPIError("fetch", 404)

    class Planner(ScriptedModel):
        async def choose(self, messages, tools):
            context = json.loads(messages[1].content)
            assert [h["url"] for h in context["web_leads"]] == ["https://example.org/alternative"]
            assert context["failed_tools"][0]["arguments"]["url"] == "https://example.org/failed"
            assert "404" in context["failed_tools"][0]["error"]
            assert context["remaining_web_requests"] == {"search": 2, "fetch": 3}
            return AIMessage(content="", tool_calls=[])

    monkeypatch.setattr(agent_module, "search_web", search)
    monkeypatch.setattr(web, "fetch", fetch)
    agent = ResearchAgent(config(), Planner(), corpus, nop, nop, nop)
    agent.mode = "online"
    await agent.tools["search_web"].ainvoke({"query": "TEST"})
    state = {"messages": [], "tool_calls": 1, "rounds": 0, "limits": []}
    state.update(
        await agent.execute(
            {
                **state,
                "selected": [
                    {"name": "fetch_page", "args": {"url": "https://example.org/failed"}, "id": "1"}
                ],
            }
        )
    )
    await agent.choose({**state, "question": "TEST", "analysis": {}, "mode": "online"})


async def test_fetch_rejects_observed_antibot_page_but_keeps_usage(monkeypatch):
    usage = []
    mock_http(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "title": "TEST blocked",
                "content": "Request unsuccessful. Incapsula incident ID: TEST ONLY",
            },
        ),
    )

    async def report(row):
        usage.append(row)

    with pytest.raises(FetchError, match="访问拦截页"):
        await web.fetch("https://example.org/", config(), report)
    assert usage[0]["billable"] is True and usage[0]["estimated_cost_cny"] == "0.005"


@pytest.mark.parametrize("status", [401, 402])
async def test_tool_account_error_stops_worker_and_preserves_meter(monkeypatch, sf, corpus, status):
    import fakeredis.aioredis

    from shijian import agent as agent_module
    from shijian.tasks import TaskStore
    from shijian.worker import execute_task

    calls = []

    async def rejected_search(query, cfg, on_usage):
        calls.append(query)
        await on_usage(
            {"kind": "search", "billable": False, "estimated_cost_cny": "0.000", "http_status": status}
        )
        raise web.WebAPIError("search", status)

    class SearchModel(ScriptedModel):
        async def choose(self, messages, tools):
            return AIMessage(
                content="", tool_calls=[{"name": "search_web", "args": {"query": "TEST"}, "id": "1"}]
            )

    monkeypatch.setattr(agent_module, "search_web", rejected_search)
    store = TaskStore(sf)
    bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
    task_id = store.create("TEST online account error", "online")
    await execute_task(task_id, "test-worker", config(), store, bus, corpus, lambda _: SearchModel())
    task = store.get(task_id)
    assert task["status"] == "failed" and task["result"] is None
    assert str(status) in task["error"] and len(calls) == 1
    diagnostic = next(e["data"] for e in store.events(task_id) if e["type"] == "diagnostic")
    assert len(diagnostic["external_requests"]) == 1
    await bus.aclose()
