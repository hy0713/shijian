"""Collection/index/scope contracts; no paid APIs or historical inference in these tests."""

import fakeredis.aioredis
import pytest
import redis.exceptions
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select

from shijian import api
from shijian.config import Settings, settings
from shijian.corpus import Corpus
from shijian.db import CollectionSource, Passage, Source, Task, utcnow
from shijian.indexing import IndexStore, process_index
from shijian.schema import SearchFilter
from shijian.tasks import TaskStore
from shijian.worker import execute_task
from tests.fakes import ScriptedModel


def test_upgrade_preserves_existing_sources_and_task_scope(monkeypatch, tmp_path):
    path = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + path.as_posix())
    settings.cache_clear()
    engine = create_engine("sqlite:///" + path.as_posix())
    try:
        command.upgrade(Config("alembic.ini"), "0002")
        old = MetaData()
        old.reflect(engine)
        with engine.begin() as conn:
            conn.execute(
                old.tables["sources"]
                .insert()
                .values(
                    id="legacy-source",
                    title="TEST preserved original",
                    source_type="其他",
                    raw_text="TEST ONLY 原文必须保留，不声称古籍。",
                    content_hash="x" * 64,
                    people=[],
                    topics=[],
                    provenance_note="TEST",
                    imported_at=utcnow(),
                    index_status="ready",
                )
            )
            conn.execute(
                old.tables["tasks"]
                .insert()
                .values(
                    id="legacy-task",
                    question="TEST old task",
                    mode="online",
                    status="completed",
                    cancel_requested=False,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                    result={"TEST": True},
                )
            )
        command.upgrade(Config("alembic.ini"), "head")
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            row = s.get(Source, "legacy-source")
            assert row.saved and row.content_hash == "x" * 64 and "原文必须保留" in row.raw_text
            task = s.get(Task, "legacy-task")
            assert task.source_ids is None and task.result == {"TEST": True}
    finally:
        engine.dispose()
        settings.cache_clear()


@pytest.fixture
def client(monkeypatch, sf, corpus):
    monkeypatch.setattr(api, "sessions", lambda: sf)
    monkeypatch.setattr(api, "store", lambda: TaskStore(sf))
    monkeypatch.setattr(api, "corpus", lambda: corpus)
    monkeypatch.setattr(
        api,
        "settings",
        lambda: Settings(
            _env_file=None,
            deepseek_api_key="TEST-ONLY",
            deepseek_model="TEST-ONLY",
            deepseek_base_url="https://test.invalid",
        ),
    )
    with TestClient(api.app) as client:
        client.app.state.bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
        yield client


def test_snapshot_opt_in_and_duplicate_does_not_demote(corpus, demo_docs):
    first = corpus.import_document(demo_docs[0], index=False, saved=False)
    assert first["index_status"] == "unrequested"
    sid = first["source_id"]
    assert sid not in IndexStore(corpus.sessions).pending()
    assert not corpus.eligible(SearchFilter())
    ids = corpus.page_passages(sid)
    assert ids and corpus.read(ids[0]["id"])["text"] in demo_docs[0].raw_text
    with pytest.raises(ValueError, match="先收藏"):
        IndexStore(corpus.sessions).request(sid)
    again = corpus.import_document(demo_docs[0])
    assert again["source_id"] == sid and again["duplicate"]
    assert corpus.page_passages(sid) == ids
    assert sid in corpus.eligible(SearchFilter())
    assert corpus.import_document(demo_docs[0], index=False, saved=False)["index_status"] == "ready"
    assert sid in corpus.eligible(SearchFilter())


async def test_bookmark_index_retry_and_unbookmark_keeps_evidence(client, corpus, demo_docs):
    sid = corpus.import_document(demo_docs[0], index=False, saved=False)["source_id"]
    store = TaskStore(corpus.sessions)
    tid = store.create("TEST preserved citation", "local")
    assert store.claim(tid, "w", 60)
    evidence = corpus.read(corpus.page_passages(sid)[0]["id"])
    store.remember(tid, "w", evidence)
    before = store.evidence(tid)
    assert client.post(f"/api/sources/{sid}/index").status_code == 409
    for _ in range(2):
        r = client.put(f"/api/sources/{sid}/bookmark", json={"saved": True})
        assert r.status_code == 200 and r.json()["index_status"] == "pending"
    corpus.vectors.fail = True
    await process_index(sid, corpus, IndexStore(corpus.sessions))
    assert client.get(f"/api/sources/{sid}").json()["index_status"] == "failed"
    corpus.vectors.fail = False
    assert client.post(f"/api/sources/{sid}/index").status_code == 202
    await process_index(sid, corpus, IndexStore(corpus.sessions))
    assert corpus.search("史思明")
    assert client.put(f"/api/sources/{sid}/bookmark", json={"saved": False}).status_code == 200
    assert not corpus.search("史思明")
    assert store.evidence(tid) == before
    assert corpus.read(evidence["passage_id"])["text"] == evidence["text"]
    assert not client.get("/api/sources?saved=true").json()
    assert client.get("/api/sources?saved=false").json()[0]["source_id"] == sid
    assert client.put("/api/sources/missing/bookmark", json={"saved": True}).status_code == 404


def test_collection_membership_metadata_filters_and_preservation(client, corpus, demo_docs):
    sid = corpus.import_document(demo_docs[0], index=False, saved=False)["source_id"]
    store = TaskStore(corpus.sessions)
    tid = store.create("TEST historical record", "local")
    timestamp = store.get(tid)["updated_at"]
    body = {"name": "  条约研究  ", "people": ["李鸿章"], "events": ["辛丑條約"], "regions": ["北京"]}
    cid = client.post("/api/collections", json=body).json()["id"]
    assert client.get("/api/collections?event=辛丑条约&region=北京&person=李鸿章").json()[0]["id"] == cid
    for _ in range(2):
        assert client.put(f"/api/collections/{cid}/sources/{sid}").status_code == 200
        assert client.put(f"/api/collections/{cid}/tasks/{tid}").status_code == 200
    detail = client.get(f"/api/collections/{cid}").json()
    assert detail["name"] == "条约研究" and detail["source_count"] == detail["task_count"] == 1
    assert "raw_text" not in detail["sources"][0] and "result" not in detail["tasks"][0]
    assert detail["sources"][0]["saved"] and detail["sources"][0]["index_status"] == "pending"
    assert store.get(tid)["updated_at"] == timestamp
    assert (
        client.put(f"/api/collections/{cid}", json={**body, "description": "TEST scope"}).status_code == 200
    )
    assert client.delete(f"/api/collections/{cid}/sources/{sid}").status_code == 200
    assert client.get(f"/api/sources/{sid}").json()["saved"]
    assert client.delete(f"/api/collections/{cid}").status_code == 200
    assert client.get(f"/api/collections/{cid}").status_code == 404
    assert client.get(f"/api/sources/{sid}").status_code == 200 and store.get(tid)["updated_at"] == timestamp
    assert client.post("/api/collections", json={"name": "  "}).status_code == 422
    assert client.post("/api/collections", json={"name": "TEST", "people": [" "]}).status_code == 422
    assert client.put(f"/api/collections/{cid}/tasks/{tid}").status_code == 404


async def test_collection_scope_frozen_and_worker_isolated(client, sf, corpus, demo_docs):
    ids = [corpus.import_document(d)["source_id"] for d in demo_docs[:2]]
    cid = client.post("/api/collections", json={"name": "TEST scoped research"}).json()["id"]
    client.put(f"/api/collections/{cid}/sources/{ids[0]}")
    r = client.post(
        "/api/tasks", json={"question": "TEST fixed evidence range", "mode": "local", "collection_id": cid}
    )
    assert r.status_code == 201, r.text
    tid = r.json()["task_id"]
    client.put(f"/api/collections/{cid}/sources/{ids[1]}")
    client.delete(f"/api/collections/{cid}/sources/{ids[0]}")
    task = TaskStore(sf).get(tid)
    assert task["source_ids"] == [ids[0]]
    bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await execute_task(
        tid, "scope-test", Settings(_env_file=None), TaskStore(sf), bus, corpus, lambda _: ScriptedModel()
    )
    assert TaskStore(sf).get(tid)["status"] == "completed"
    assert {e["source_id"] for e in TaskStore(sf).evidence(tid)} == {ids[0]}
    assert corpus.source_scope is None and set(corpus.eligible(SearchFilter())) == set(ids)
    # Even an explicit malicious tool filter is intersected with the task's immutable scope.
    assert not Corpus(sf, corpus.vectors, [ids[0]]).search("史思明", SearchFilter(source_ids=[ids[1]]))
    assert client.get(f"/api/collections/{cid}").json()["task_count"] == 1
    await bus.aclose()


def test_empty_unready_or_online_scope_rejected_without_task(client, sf, corpus, demo_docs):
    cid = client.post("/api/collections", json={"name": "TEST empty scope"}).json()["id"]
    base = {"question": "TEST no implicit whole-corpus fallback", "mode": "local", "collection_id": cid}
    assert client.post("/api/tasks", json=base).status_code == 409
    sid = corpus.import_document(demo_docs[0], index=False)["source_id"]
    client.put(f"/api/collections/{cid}/sources/{sid}")
    assert client.post("/api/tasks", json=base).status_code == 409
    # Check the store boundary too; web config validation may precede HTTP scope checks.
    with pytest.raises(ValueError, match="仅用于限定"):
        TaskStore(sf).create("TEST", "online", collection_id=cid)
    assert (
        client.post("/api/tasks", json={"question": "TEST", "mode": "local", "source_ids": []}).status_code
        == 422
    )
    with sf() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 0
        assert s.scalar(select(func.count()).select_from(CollectionSource)) == 1


async def test_stale_scope_fails_before_model(client, sf, corpus, demo_docs):
    sid = corpus.import_document(demo_docs[0])["source_id"]
    tid = TaskStore(sf).create("TEST source later unsaved", "local", [sid], embedding_key=corpus.vectors.key)
    client.put(f"/api/sources/{sid}/bookmark", json={"saved": False})

    def never_model(_):
        raise AssertionError("must fail before any provider request")

    bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await execute_task(tid, "scope-test", Settings(_env_file=None), TaskStore(sf), bus, corpus, never_model)
    result = TaskStore(sf).get(tid)
    assert result["status"] == "failed" and "未调用模型" in result["error"]
    with sf() as s:
        assert s.scalar(select(func.count()).select_from(Passage).where(Passage.source_id == sid)) > 0
    await bus.aclose()


def test_unsave_pending_cancels_delivery_and_redis_loss_is_recoverable(
    client, monkeypatch, corpus, demo_docs
):
    sid = corpus.import_document(demo_docs[0], index=False, saved=False)["source_id"]

    async def unavailable(*args):
        raise redis.exceptions.ConnectionError("TEST Redis unavailable")

    monkeypatch.setattr(api, "enqueue_index", unavailable)
    r = client.put(f"/api/sources/{sid}/bookmark", json={"saved": True})
    assert r.status_code == 200 and r.json()["queued"] is False
    index = IndexStore(corpus.sessions)
    assert sid in index.pending()
    client.put(f"/api/sources/{sid}/bookmark", json={"saved": False})
    assert sid not in index.pending() and not index.claim(sid, "stale-delivery")
    assert client.get(f"/api/sources/{sid}").json()["index_status"] == "unrequested"
    client.put(f"/api/sources/{sid}/bookmark", json={"saved": True})
    assert sid in index.pending()  # MySQL truth can repair the lost Redis hint.
