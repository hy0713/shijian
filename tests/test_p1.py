import json
from datetime import timedelta

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from shijian import api, books
from shijian.config import Settings
from shijian.db import Passage, Source, utcnow
from shijian.indexing import IndexStore, LostIndexLease, process_index
from shijian.web import FetchError, search_web


async def test_async_index_partial_failure_retry_and_dedup(corpus, demo_docs):
    imported = corpus.import_document(demo_docs[0], index=False)
    sid = imported["source_id"]
    store = IndexStore(corpus.sessions)
    upsert = corpus.vectors.upsert
    calls = []

    def fail_second(batch):
        calls.append(len(batch))
        if len(calls) == 2:
            raise ConnectionError("TEST private-error-body")
        upsert(batch)

    corpus.vectors.upsert = fail_second
    await process_index(sid, corpus, store)
    with corpus.sessions() as s:
        row = s.get(Source, sid)
        assert row.index_status == "failed" and row.indexed_passages == 32
        assert "private-error-body" not in row.index_error
        total = s.scalar(select(func.count()).select_from(Passage).where(Passage.source_id == sid))
    assert not corpus.search("史思明")
    corpus.vectors.upsert = upsert
    store.request(sid)
    await process_index(sid, corpus, store)
    assert not await process_index(sid, corpus, store)
    with corpus.sessions() as s:
        row = s.get(Source, sid)
        assert row.index_status == "ready" and row.indexed_passages == row.index_total == total
        assert len(corpus.vectors.rows) == total


def test_index_lease_recovery_cannot_overwrite_new_owner(corpus, demo_docs):
    sid = corpus.import_document(demo_docs[0], index=False)["source_id"]
    store = IndexStore(corpus.sessions)
    assert store.claim(sid, "old") and not store.claim(sid, "other")
    with corpus.sessions.begin() as s:
        s.get(Source, sid).index_lease_until = utcnow() - timedelta(seconds=1)
    assert store.reap() == 1
    store.request(sid)
    assert store.claim(sid, "new")
    with pytest.raises(LostIndexLease):
        store.progress(sid, "old", 5, 10)
    store.fail(sid, "old", "stale failure")
    with corpus.sessions() as s:
        assert s.get(Source, sid).index_owner == "new" and s.get(Source, sid).index_error is None


def test_library_filters_metadata_and_retry(monkeypatch, sf, corpus, demo_docs):
    for doc in demo_docs:
        corpus.import_document(doc, index=False)
    monkeypatch.setattr(api, "sessions", lambda: sf)
    monkeypatch.setattr(api, "corpus", lambda: corpus)
    with TestClient(api.app) as client:
        client.app.state.bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
        rows = client.get(
            "/api/sources?query=新唐书&person=史思明&topic=安史之乱&index_status=pending"
        ).json()
        assert len(rows) == 1 and rows[0]["title"] == "新唐書" and "raw_text" not in rows[0]
        sid = rows[0]["source_id"]
        raw = client.get("/api/sources/" + sid).json()["raw_text"]
        assert client.patch("/api/sources/" + sid, json={"raw_text": "forbidden"}).status_code == 422
        assert client.patch("/api/sources/" + sid, json={"title": None}).status_code == 422
        assert (
            client.patch("/api/sources/" + sid, json={"topics": ["安史之乱", "测试专题"]}).status_code == 200
        )
        assert client.get("/api/sources?topic=测试专题").json()[0]["source_id"] == sid
        assert client.get("/api/sources/" + sid).json()["raw_text"] == raw
        assert client.post("/api/sources/" + sid + "/index").status_code == 202
        assert client.post("/api/sources/missing/index").status_code == 404


def test_fixed_revision_reuses_saved_snapshot(monkeypatch, sf, corpus, demo_docs):
    doc = demo_docs[0]
    saved = corpus.import_document(doc, index=False)
    monkeypatch.setattr(api, "sessions", lambda: sf)

    async def must_not_fetch(*args):
        raise AssertionError("immutable saved revision must not require the network")

    monkeypatch.setattr(api, "fetch_page", must_not_fetch)
    with TestClient(api.app) as client:
        client.app.state.bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
        response = client.post("/api/sources/page", json={"url": str(doc.source_url)})
        assert response.status_code == 201
        assert response.json()["cached_revision"] and response.json()["duplicate"]
        assert response.json()["source_id"] == saved["source_id"]


async def test_wikisource_pins_revision_and_strips_html(monkeypatch):
    requests = []

    async def fetch(url, config):
        requests.append(url)
        if "/search/page?" in url:
            return {
                "pages": [
                    {
                        "key": "新唐書/卷225上",
                        "title": "新唐書/卷225上",
                        "excerpt": "<span>史思明</span><script>bad()</script>",
                    }
                ]
            }
        return {
            "title": "新唐書/卷225上",
            "latest": {"id": 123},
            "license": {"title": "CC BY-SA"},
            "html": "<html><body><article>與安祿山共鄉里，生先祿山一日，故長相善。<script>bad()</script></article></body></html>",
        }

    monkeypatch.setattr(books, "fetch_json", fetch)
    c = Settings(_env_file=None, search_provider="wikisource")
    hits = await search_web("史思明", c)
    assert c.web_ready() and hits[0]["url"].endswith("%E5%8D%B7225%E4%B8%8A")
    page = await books.read_wikisource(hits[0]["url"], c)
    assert "oldid=123" in page["url"] and "CC BY-SA" in page["provenance_note"]
    assert "bad()" not in page["text"] and page["revision"] == 123
    assert "%2F" in requests[-1]
    with pytest.raises(FetchError):
        await books.read_wikisource("https://example.com/wiki/abc", c)


@pytest.mark.parametrize(
    "payload", [[], {"latest": None, "html": "x"}, {"title": "书", "latest": {"id": 0}, "html": "x"}]
)
async def test_malformed_book_metadata_is_not_imported(monkeypatch, payload):
    async def fetch(*args):
        return payload

    monkeypatch.setattr(books, "fetch_json", fetch)
    with pytest.raises(FetchError):
        await books.read_wikisource("https://zh.wikisource.org/wiki/test", Settings(_env_file=None))


def test_evaluation_api_requires_explicit_human_scores(monkeypatch, tmp_path):
    root = tmp_path / "runs"
    target = root / "test-run"
    target.mkdir(parents=True)
    row = {
        "question_id": "Q01",
        "question": "TEST",
        "category": "TEST",
        "method": "agent",
        "result": {
            "metrics": {"token_usage": {"TEST": {"total_tokens": 2}}, "tool_calls": 1},
            "citations": [],
        },
        "human_review": {
            "reviewer": None,
            "evidence_support_0_to_2": None,
            "completeness_0_to_2": None,
            "premise_recognition_0_to_2": None,
            "notes": None,
        },
    }
    p = target / "Q01_agent.json"
    p.write_text(json.dumps(row), "utf-8")
    from shijian import evaluations

    monkeypatch.setattr(evaluations, "settings", lambda: Settings(_env_file=None, evaluation_dir=str(root)))
    with TestClient(api.app) as client:
        assert client.get("/api/evaluations").json()[0]["records"] == 1
        assert (
            client.get("/api/evaluations/test-run").json()["records"][0]["human_review"]["reviewer"] is None
        )
        path = "/api/evaluations/test-run/Q01/agent/review"
        assert client.put(path, json={"reviewer": "TEST"}).status_code == 422
        scores = {
            "reviewer": "TEST reviewer",
            "evidence_support_0_to_2": 1,
            "completeness_0_to_2": 1,
            "premise_recognition_0_to_2": 2,
            "notes": "TEST ONLY",
        }
        assert client.put(path, json=scores).json() == scores
        assert json.loads(p.read_text("utf-8"))["human_review"] == scores
        assert client.get("/api/evaluations/.hidden").status_code == 400
        assert client.get("/api/evaluations/test-run/Q01/invalid").status_code == 400


def test_evaluation_summary_keeps_failed_usage_and_unreviewed_scores(tmp_path):
    from eval.summarize import summarize

    (tmp_path / "Q01_direct.json").write_text(
        json.dumps(
            {
                "question_id": "Q01",
                "method": "direct",
                "elapsed_seconds": 2,
                "result": {"metrics": {"tool_calls": 0, "token_usage": {"TEST": {"total_tokens": 4}}}},
                "automatic": {"evidence_anchor_recall": None},
                "human_review": {"reviewer": None, "completeness_0_to_2": 2},
            }
        ),
        "utf-8",
    )
    (tmp_path / "Q01_agent.json").write_text(
        json.dumps(
            {
                "question_id": "Q01",
                "method": "agent",
                "status": "failed",
                "error_type": "TEST",
                "metrics": {"token_usage": {"TEST": {"total_tokens": 7}}},
            }
        ),
        "utf-8",
    )
    report = summarize(tmp_path)
    assert report["methods"]["agent"]["failed"] == 1
    assert report["methods"]["agent"]["observed_total_tokens"] == 7
    assert report["methods"]["agent"]["mean_elapsed_seconds"] is None
    assert report["methods"]["direct"]["human_only"]["completeness_0_to_2"]["mean"] is None
    assert len(report["attention_cases"]) == 1
