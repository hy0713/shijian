"""Index worker: Redis delivery, MySQL truth, leased idempotent Milvus upserts."""

import asyncio
import contextlib
import logging
from datetime import timedelta
from uuid import uuid4

import redis.asyncio as redis
from sqlalchemy import func, select, update

from shijian.db import Passage, Source, utcnow

QUEUE = "shijian:index-queue"
log = logging.getLogger("shijian.indexing")


class LostIndexLease(RuntimeError):
    pass


class IndexStore:
    def __init__(self, sessions):
        self.sessions = sessions

    def request(self, source_id, force=False):
        with self.sessions.begin() as s:
            row = s.scalar(select(Source).where(Source.id == source_id).with_for_update())
            if row is None:
                raise LookupError("资料不存在")
            if not row.saved:
                raise ValueError("请先收藏这份证据快照，再建立索引")
            if row.index_status != "indexing" and (force or row.index_status != "ready"):
                row.index_status, row.index_error = "pending", None
                row.indexed_passages = 0
                row.index_total = s.scalar(
                    select(func.count()).select_from(Passage).where(Passage.source_id == source_id)
                )
            return {"source_id": source_id, "index_status": row.index_status}

    def pending(self):
        with self.sessions() as s:
            return list(
                s.scalars(
                    select(Source.id)
                    .where(Source.index_status == "pending", Source.saved.is_(True))
                    .order_by(Source.imported_at)
                    .limit(100)
                )
            )

    def claim(self, source_id, owner):
        with self.sessions.begin() as s:
            return bool(
                s.execute(
                    update(Source)
                    .where(Source.id == source_id, Source.index_status == "pending", Source.saved.is_(True))
                    .values(
                        index_status="indexing",
                        index_owner=owner,
                        index_error=None,
                        index_lease_until=utcnow() + timedelta(seconds=120),
                        indexed_passages=0,
                    )
                ).rowcount
            )

    def progress(self, source_id, owner, done, total):
        with self.sessions.begin() as s:
            won = s.execute(
                update(Source)
                .where(Source.id == source_id, Source.index_owner == owner, Source.index_status == "indexing")
                .values(
                    indexed_passages=done,
                    index_total=total,
                    index_lease_until=utcnow() + timedelta(seconds=120),
                )
            ).rowcount
            if not won:
                raise LostIndexLease("索引租约已失效")

    def heartbeat(self, source_id, owner):
        with self.sessions.begin() as s:
            s.execute(
                update(Source)
                .where(Source.id == source_id, Source.index_owner == owner, Source.index_status == "indexing")
                .values(index_lease_until=utcnow() + timedelta(seconds=120))
            )

    def fail(self, source_id, owner, error):
        with self.sessions.begin() as s:
            s.execute(
                update(Source)
                .where(Source.id == source_id, Source.index_owner == owner, Source.index_status == "indexing")
                .values(index_status="failed", index_error=error, index_owner=None, index_lease_until=None)
            )

    def reap(self):
        with self.sessions.begin() as s:
            return s.execute(
                update(Source)
                .where(Source.index_status == "indexing", Source.index_lease_until < utcnow())
                .values(
                    index_status="failed",
                    index_error="索引 worker 租约过期，请重试；原文已保留。",
                    index_owner=None,
                    index_lease_until=None,
                )
            ).rowcount


async def enqueue_index(bus, source_id):
    if await bus.set("shijian:index-queued:" + source_id, "1", nx=True, ex=60):
        await bus.lpush(QUEUE, source_id)


async def process_index(source_id, corpus, store):
    owner = str(uuid4())
    if not await asyncio.to_thread(store.claim, source_id, owner):
        return False

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            await asyncio.to_thread(store.heartbeat, source_id, owner)

    job = asyncio.create_task(heartbeat())
    try:
        await asyncio.to_thread(corpus.index_source, source_id, owner)
    except LostIndexLease:
        log.warning("index_lease_lost source=%s", source_id)
    except Exception as exc:
        # Worker boundary isolates one failed index; SDK error bodies may include credentials.
        await asyncio.to_thread(
            store.fail,
            source_id,
            owner,
            "索引失败（" + type(exc).__name__ + "）；检查 Embedding 与 Milvus 后重试，原文已保留。",
        )
        log.error("index_failed source=%s type=%s", source_id, type(exc).__name__)
    finally:
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job
    return True


async def run_index_worker():
    from shijian.config import settings
    from shijian.corpus import Corpus
    from shijian.db import sessions
    from shijian.vectors import Vectors

    config = settings()
    store = IndexStore(sessions())
    corpus = Corpus(sessions(), Vectors(config))
    bus = redis.from_url(config.redis_url, decode_responses=True, socket_timeout=5)
    try:
        while True:
            await asyncio.to_thread(store.reap)
            try:
                for source_id in await asyncio.to_thread(store.pending):
                    await enqueue_index(bus, source_id)
                item = await bus.brpop(QUEUE, timeout=3)
                if item:
                    await bus.delete("shijian:index-queued:" + item[1])
                    await process_index(item[1], corpus, store)
            except redis.RedisError:
                log.warning("index_queue_unavailable")
                await asyncio.sleep(3)
    finally:
        await bus.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_index_worker())
