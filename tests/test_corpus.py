import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from shijian.corpus import split_text, terms
from shijian.db import Passage, Source, SourceLocation
from shijian.schema import SearchFilter


def test_real_corpus_hashes_and_transcription(demo_docs):
    provenance = json.loads(Path("data/demo/provenance.json").read_text("utf-8"))
    assert len(demo_docs) == len(provenance) == 6
    for doc, row in zip(demo_docs, provenance):
        assert hashlib.sha256(doc.raw_text.encode()).hexdigest() == row["raw_sha256"]
        assert row["revision_id"] in str(doc.source_url)
        assert "史思明" in doc.raw_text


def test_import_preserves_offsets_links_and_dedup(corpus, demo_docs):
    doc = demo_docs[0]
    first = corpus.import_document(doc)
    second = corpus.import_document(
        doc.model_copy(update={"source_url": "https://example.org/reprint?utm_source=x"})
    )
    assert first["source_id"] == second["source_id"] and second["duplicate"]
    with corpus.sessions() as s:
        source = s.get(Source, first["source_id"])
        assert source.raw_text == doc.raw_text
        ps = list(s.scalars(select(Passage).order_by(Passage.ordinal)))
        assert s.scalar(select(func.count()).select_from(Source)) == 1
        assert s.scalar(select(func.count()).select_from(SourceLocation)) == 2
        for i, p in enumerate(ps):
            assert p.text == doc.raw_text[p.start : p.end]
            assert p.prev_id == (ps[i - 1].id if i else None)
            assert p.next_id == (ps[i + 1].id if i < len(ps) - 1 else None)
            e = corpus.read(p.id)
            assert e["text"] == p.text
            assert e["before"] == (ps[i - 1].text if i else "")


def test_keyword_traditional_simplified_and_filters(corpus, demo_docs):
    for d in demo_docs[:2]:
        corpus.import_document(d)
    hits = corpus.search("安庆绪 史思明", SearchFilter(people=["史思明"], topics=["安史之乱"]))
    assert hits and "思明" in corpus.read(hits[0]["passage_id"])["text"]
    assert not corpus.search("安禄山", SearchFilter(people=["拿破仑"]))
    sid = hits[0]["source_id"]
    assert all(h["source_id"] == sid for h in corpus.search("安禄山", SearchFilter(source_ids=[sid])))
    assert "安庆绪" in terms("安慶緒")


def test_index_failure_is_repairable(corpus, demo_docs):
    corpus.vectors.fail = True
    with pytest.raises(ConnectionError):
        corpus.import_document(demo_docs[0])
    with corpus.sessions() as s:
        source = s.scalar(select(Source))
        assert source.index_status == "pending"
        sid = source.id
        count = s.scalar(select(func.count()).select_from(Passage))
    assert corpus.search("史思明") == []
    corpus.vectors.fail = False
    repaired = corpus.import_document(demo_docs[0])
    assert repaired["duplicate"] and repaired["source_id"] == sid
    with corpus.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Passage)) == count
        assert s.get(Source, sid).index_status == "ready"


def test_chunk_semantic_boundaries():
    text = "甲乙。丙丁；\n\n" + "史思明" * 200
    spans = split_text(text, 50)
    assert all(b - a <= 50 for a, b in spans)
    assert "".join(text[a:b] for a, b in spans) == text.replace("\n", "")


def test_whitespace_original_rejected(demo_docs):
    from shijian.schema import DocumentInput

    with pytest.raises(ValidationError):
        DocumentInput.model_validate({**demo_docs[0].model_dump(), "raw_text": " " * 30})
