"""SQLite and deterministic vectors are TEST DOUBLES only; production has no fallback backend."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shijian.corpus import Corpus
from shijian.db import Base
from shijian.schema import DocumentInput


class FakeVectors:
    key = "TEST-DOUBLE-vectors"

    def __init__(self):
        self.rows = {}
        self.fail = False

    def upsert(self, passages):
        if self.fail:
            raise ConnectionError("TEST-DOUBLE-Milvus unavailable")
        self.rows.update({p.id: p for p in passages})

    def search(self, query, source_ids, limit):
        # No semantic capability is claimed. Unit tests exercise lexical retrieval + fusion contract.
        return []


@pytest.fixture
def sf():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def corpus(sf):
    return Corpus(sf, FakeVectors())


@pytest.fixture
def demo_docs():
    return [
        DocumentInput.model_validate(d) for d in json.loads(Path("data/demo/corpus.json").read_text("utf-8"))
    ]
