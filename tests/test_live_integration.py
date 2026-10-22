"""Explicit live-service contract checks. Use a dedicated *_test MySQL database, never production."""

import os
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis.asyncio as redis
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import Collection, CollectionSource, Passage, Source
from shijian.indexing import QUEUE as INDEX_QUEUE
from shijian.indexing import IndexStore, enqueue_index, process_index
from shijian.schema import DocumentInput, SearchFilter
from shijian.tasks import TaskStore, enqueue
from shijian.vectors import Vectors
from shijian.worker import execute_task
from tests.fakes import ScriptedModel

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL"),
        reason="Requires explicit TEST_DATABASE_URL (*_test), Redis, Milvus, downloaded BGE",
    ),
]


def test_live_mysql_keyword_aggregate_type(monkeypatch, demo_docs):
    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).drivername.startswith("mysql") and make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    settings.cache_clear()
    engine = create_engine(url, pool_pre_ping=True)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        corpus = Corpus(sessionmaker(engine, expire_on_commit=False), Vectors(settings()))
        imported = corpus.import_document(demo_docs[0], index=False)
        hits = corpus.keyword("史思明 安庆绪", [imported["source_id"]], 3)
        assert hits and len(hits) <= 3
        assert corpus.read(hits[0])["source_id"] == imported["source_id"]
    finally:
        engine.dispose()
        settings.cache_clear()


async def test_live_opt_in_snapshot_collection_and_scoped_worker(monkeypatch):
    from uuid import uuid4

    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    settings.cache_clear()
    config = settings().model_copy(update={"milvus_collection": "shijian_integration_bge_zh_v1"})
    engine = create_engine(url, pool_pre_ping=True)
    bus = redis.from_url(urlunsplit(urlsplit(config.redis_url)._replace(path="/1")), decode_responses=True)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        sf = sessionmaker(engine, expire_on_commit=False)
        corpus = Corpus(sf, Vectors(config))
        doc = DocumentInput(
            title="TEST ONLY P2 snapshot fixture",
            source_type="其他",
            raw_text="TEST ONLY protocol fixture: 史思明 安庆绪。This is not a historical claim. "
            + str(uuid4()),
        )
        sid = corpus.import_document(doc, index=False, saved=False)["source_id"]
        assert sid not in IndexStore(sf).pending()
        ids = corpus.page_passages(sid)
        cid = str(uuid4())
        with sf.begin() as s:
            s.add(Collection(id=cid, name="TEST ONLY scoped collection"))
            s.flush()
            s.add(CollectionSource(collection_id=cid, source_id=sid))
            s.get(Source, sid).saved = True
        IndexStore(sf).request(sid)
        assert await process_index(sid, corpus, IndexStore(sf))
        assert corpus.import_document(doc, index=False, saved=False)["source_id"] == sid
        assert corpus.page_passages(sid) == ids
        store = TaskStore(sf)
        tid = store.create(
            "TEST ONLY P2 scoped worker", "local", collection_id=cid, embedding_key=corpus.vectors.key
        )
        await execute_task(tid, "p2-live-scope", config, store, bus, corpus, lambda _: ScriptedModel())
        assert store.get(tid)["status"] == "completed"
        assert {e["source_id"] for e in store.evidence(tid)} == {sid}
        before = store.evidence(tid)
        with sf.begin() as s:
            s.get(Source, sid).saved = False
        assert not Corpus(sf, corpus.vectors, [sid]).eligible(SearchFilter())
        assert store.evidence(tid) == before and corpus.read(ids[0]["id"])["text"] == doc.raw_text
    finally:
        await bus.aclose()
        engine.dispose()
        settings.cache_clear()


async def test_live_mysql_milvus_redis_idempotency_and_worker(monkeypatch, demo_docs):
    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).drivername.startswith("mysql") and make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    settings.cache_clear()
    base = settings()
    redis_parts = urlsplit(base.redis_url)
    test_redis = os.getenv("TEST_REDIS_URL") or urlunsplit(redis_parts._replace(path="/1"))
    config = base.model_copy(
        update={"milvus_collection": "shijian_integration_bge_zh_v1", "redis_url": test_redis}
    )
    engine = create_engine(url, pool_pre_ping=True)
    bus = redis.from_url(config.redis_url, decode_responses=True)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        sf = sessionmaker(engine, expire_on_commit=False)
        corpus = Corpus(sf, Vectors(config))
        await bus.ping()
        first = corpus.import_document(demo_docs[0], index=False)
        index_store = IndexStore(sf)
        index_store.request(first["source_id"], force=True)
        await enqueue_index(bus, first["source_id"])
        delivery = await bus.brpop(INDEX_QUEUE, timeout=3)
        assert delivery and delivery[1] == first["source_id"]
        assert await process_index(delivery[1], corpus, index_store)
        assert not await process_index(delivery[1], corpus, index_store)
        with sf() as s:
            indexed = s.get(Source, first["source_id"])
            assert indexed.index_status == "ready"
            assert indexed.indexed_passages == indexed.index_total > 0
        assert corpus.import_document(demo_docs[0])["source_id"] == first["source_id"]
        hits = corpus.search("史思明 安庆绪")
        assert hits
        e = corpus.read(hits[0]["passage_id"])
        with sf() as s:
            p = s.get(Passage, e["passage_id"])
            assert p.source_id == e["source_id"]
        vectors = corpus.vectors.client.query(
            config.milvus_collection, filter='id == "' + e["passage_id"] + '"', output_fields=["source_id"]
        )
        assert vectors[0]["source_id"] == e["source_id"]
        store = TaskStore(sf)
        tid = store.create("Live infrastructure test using scripted model", "local")
        await enqueue(bus, tid)
        assert await bus.get(f"shijian:queued:{tid}")
        await execute_task(tid, "integration-worker", config, store, bus, corpus, lambda _: ScriptedModel())
        assert store.get(tid)["status"] == "completed"
        assert store.evidence(tid)
    finally:
        await bus.aclose()
        engine.dispose()
        settings.cache_clear()
