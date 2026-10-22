from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from shijian.config import settings


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    author: Mapped[str | None] = mapped_column(String(250))
    source_type: Mapped[str] = mapped_column(String(30))
    chapter: Mapped[str | None] = mapped_column(String(250))
    edition: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    people: Mapped[list] = mapped_column(JSON)
    topics: Mapped[list] = mapped_column(JSON)
    provenance_note: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    index_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    embedding_key: Mapped[str | None] = mapped_column(String(500))
    index_error: Mapped[str | None] = mapped_column(String(500))
    indexed_passages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    index_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    index_owner: Mapped[str | None] = mapped_column(String(36))
    index_lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    saved: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class SourceLocation(Base):
    __tablename__ = "source_locations"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(250))
    __table_args__ = (UniqueConstraint("source_id", "url_hash"),)


class Passage(Base):
    __tablename__ = "passages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    prev_id: Mapped[str | None] = mapped_column(String(36))
    next_id: Mapped[str | None] = mapped_column(String(36))
    tokens_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("source_id", "ordinal"),)


class Posting(Base):
    __tablename__ = "postings"
    token: Mapped[str] = mapped_column(String(100), primary_key=True)
    passage_id: Mapped[str] = mapped_column(ForeignKey("passages.id"), primary_key=True)
    frequency: Mapped[int] = mapped_column(Integer)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    source_ids: Mapped[list | None] = mapped_column(JSON)
    __table_args__ = (Index("ix_task_status_lease", "status", "lease_until"),)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    data: Mapped[dict] = mapped_column(JSON)


class TaskEvidence(Base):
    __tablename__ = "task_evidence"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    passage_id: Mapped[str] = mapped_column(ForeignKey("passages.id"))
    snapshot: Mapped[dict] = mapped_column(JSON)


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(1000), default="")
    people: Mapped[list] = mapped_column(JSON, default=list)
    events: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CollectionSource(Base):
    __tablename__ = "collection_sources"
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)


class CollectionTask(Base):
    __tablename__ = "collection_tasks"
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True, pool_recycle=1800)


def sessions():
    return sessionmaker(engine(), expire_on_commit=False)
