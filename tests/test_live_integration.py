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
from shijian.db import Passage
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
        first = corpus.import_document(demo_docs[0])
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
