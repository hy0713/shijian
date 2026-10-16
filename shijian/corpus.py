"""MySQL preserves originals; jieba + Chinese bigram postings implement the lexical baseline."""

import hashlib
import math
import re
from collections import Counter, defaultdict
from uuid import uuid4

import jieba
from opencc import OpenCC
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from shijian.db import Passage, Posting, Source, SourceLocation
from shijian.schema import DocumentInput, SearchFilter
from shijian.web import canonical_url

_cc = OpenCC("t2s")
for _name in ("安禄山", "安庆绪", "史思明", "史朝义", "李光弼", "郭子仪", "乌承恩"):
    jieba.add_word(_name)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def terms(text: str) -> list[str]:
    normalized = _cc.convert(text).lower()
    words = [w for w in jieba.cut(normalized) if re.fullmatch(r"[\w\u3400-\u9fff]{2,100}", w)]
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        words.extend(run[i : i + 2] for i in range(len(run) - 1))
    return words


def split_text(text: str, limit: int = 420) -> list[tuple[int, int]]:
    """Offsets are Python Unicode code points in the exact stored original. No overlap duplication."""
    spans = []
    for paragraph in re.finditer(r"[^\n]+", text):
        start, end = paragraph.span()
        while start < end:
            stop = min(start + limit, end)
            if stop < end:
                boundaries = list(re.finditer(r"[。！？；]", text[start:stop]))
                if boundaries and boundaries[-1].end() >= limit // 2:
                    stop = start + boundaries[-1].end()
            if text[start:stop].strip():
                spans.append((start, stop))
            start = stop
    return spans


class Corpus:
    def __init__(self, session_factory, vectors):
        self.sessions = session_factory
        self.vectors = vectors

    def import_document(self, doc: DocumentInput, index: bool = True) -> dict:
        # Whitespace-only republishing is deduplicated, but originals are never normalized in-place.
        body_hash = digest(re.sub(r"\s+", "", doc.raw_text))
        url = canonical_url(str(doc.source_url)) if doc.source_url else None
        duplicate = False
        try:
            with self.sessions.begin() as s:
                source = s.scalar(select(Source).where(Source.content_hash == body_hash))
                if source:
                    source_id, duplicate = source.id, True
                else:
                    source_id = str(uuid4())
                    fields = doc.model_dump(exclude={"source_url"})
                    source = Source(id=source_id, source_url=url, content_hash=body_hash, **fields)
                    s.add(source)
                    s.flush()
                    spans = split_text(doc.raw_text)
                    ids = [str(uuid4()) for _ in spans]
                    for i, (start, end) in enumerate(spans):
                        tokens = Counter(terms(doc.raw_text[start:end]))
                        s.add(
                            Passage(
                                id=ids[i],
                                source_id=source_id,
                                ordinal=i,
                                start=start,
                                end=end,
                                text=doc.raw_text[start:end],
                                prev_id=ids[i - 1] if i else None,
                                next_id=ids[i + 1] if i + 1 < len(ids) else None,
                                tokens_count=sum(tokens.values()),
                            )
                        )
                        s.flush()
                        s.add_all(Posting(token=t, passage_id=ids[i], frequency=n) for t, n in tokens.items())
                if url and not s.scalar(
                    select(SourceLocation.id).where(
                        SourceLocation.source_id == source_id, SourceLocation.url_hash == digest(url)
                    )
                ):
                    s.add(SourceLocation(source_id=source_id, url=url, url_hash=digest(url), title=doc.title))
        except IntegrityError:
            # A concurrent importer may win the unique body hash or location constraint.
            with self.sessions() as s:
                existing = s.scalar(select(Source).where(Source.content_hash == body_hash))
                if not existing:
                    raise
            return self.import_document(doc, index=index)
        if index:
            self.index_source(source_id)
        return {
            "source_id": source_id,
            "duplicate": duplicate,
            "index_status": "ready" if index else source.index_status,
        }

    def index_source(self, source_id: str):
        # Commit metadata before Milvus. Deterministic passage IDs make crash repair an idempotent upsert.
        with self.sessions() as s:
            source = s.get(Source, source_id)
            if source is None:
                raise LookupError("资料不存在")
            rows = list(
                s.scalars(select(Passage).where(Passage.source_id == source_id).order_by(Passage.ordinal))
            )
        self.vectors.upsert(rows)
        with self.sessions.begin() as s:
            source = s.get(Source, source_id)
            source.index_status = "ready"
            source.embedding_key = self.vectors.key

    def eligible(self, filters: SearchFilter) -> list[str]:
        with self.sessions() as s:
            rows = s.scalars(
                select(Source).where(Source.index_status == "ready", Source.embedding_key == self.vectors.key)
            )
            return [
                r.id
                for r in rows
                if (not filters.source_ids or r.id in filters.source_ids)
                and (
                    not filters.people
                    or set(map(_cc.convert, filters.people)) & set(map(_cc.convert, r.people))
                )
                and (
                    not filters.topics
                    or set(map(_cc.convert, filters.topics)) & set(map(_cc.convert, r.topics))
                )
            ]

    def keyword(self, query: str, source_ids: list[str], limit: int) -> list[str]:
        query_terms = set(terms(query))
        if not query_terms or not source_ids:
            return []
        with self.sessions() as s:
            n, avg = s.execute(
                select(func.count(), func.avg(Passage.tokens_count)).where(Passage.source_id.in_(source_ids))
            ).one()
            rows = s.execute(
                select(Posting, Passage.tokens_count)
                .join(Passage, Passage.id == Posting.passage_id)
                .where(Posting.token.in_(query_terms), Passage.source_id.in_(source_ids))
            ).all()
        # MySQL AVG(INT) returns Decimal, unlike SQLite's float.
        avg = float(avg or 1)
        df = Counter(row[0].token for row in rows)
        scores = defaultdict(float)
        for posting, length in rows:
            idf = math.log(1 + (n - df[posting.token] + 0.5) / (df[posting.token] + 0.5))
            tf = posting.frequency
            scores[posting.passage_id] += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(avg, 1)))
        return sorted(scores, key=scores.get, reverse=True)[:limit]

    def search(self, query: str, filters: SearchFilter | None = None, limit: int = 8) -> list[dict]:
        ids = self.eligible(filters or SearchFilter())
        if not ids:
            return []
        lexical = self.keyword(query, ids, 30)
        semantic = self.vectors.search(query, ids, 30)
        scores = defaultdict(float)
        for ranking in (lexical, semantic):
            for rank, pid in enumerate(ranking):
                scores[pid] += 1 / (60 + rank + 1)
        hits = []
        with self.sessions() as s:
            for pid in sorted(scores, key=scores.get, reverse=True)[:limit]:
                p = s.get(Passage, pid)
                # Stale Milvus rows never become evidence if MySQL no longer has an eligible passage.
                if p is None or p.source_id not in ids:
                    continue
                source = s.get(Source, p.source_id)
                hits.append(
                    {
                        "passage_id": pid,
                        "source_id": p.source_id,
                        "title": source.title,
                        "chapter": source.chapter,
                        "snippet": p.text[:250],
                        "score": scores[pid],
                    }
                )
        return hits

    def read(self, passage_id: str) -> dict:
        with self.sessions() as s:
            p = s.get(Passage, passage_id)
            if p is None:
                raise LookupError("片段不存在")
            source = s.get(Source, p.source_id)
            adjacent = [s.get(Passage, pid) if pid else None for pid in (p.prev_id, p.next_id)]
            neighbors = [n.text if n else "" for n in adjacent]
            return {
                "evidence_id": p.id,
                "passage_id": p.id,
                "source_id": source.id,
                "title": source.title,
                "author": source.author,
                "source_type": source.source_type,
                "chapter": source.chapter,
                "edition": source.edition,
                "url": source.source_url,
                "content_hash": source.content_hash,
                "provenance_note": source.provenance_note,
                "start": p.start,
                "end": p.end,
                "text": p.text,
                "before": neighbors[0],
                "after": neighbors[1],
                "prev_id": p.prev_id,
                "next_id": p.next_id,
                "context_passages": [
                    {
                        "evidence_id": n.id,
                        "passage_id": n.id,
                        "start": n.start,
                        "end": n.end,
                        "text": n.text,
                        "prev_id": n.prev_id,
                        "next_id": n.next_id,
                    }
                    for n in adjacent
                    if n
                ],
            }
