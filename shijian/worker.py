import asyncio
import contextlib
import logging
from uuid import uuid4

import redis.asyncio as redis

from shijian.agent import ResearchAgent
from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import sessions
from shijian.model import ResearchModel
from shijian.tasks import QUEUE, Cancelled, TaskStore, enqueue
from shijian.vectors import Vectors

log = logging.getLogger("shijian.worker")


async def execute_task(task_id, owner, config, store, bus, corpus, model_factory=ResearchModel):
    if not await asyncio.to_thread(store.claim, task_id, owner, config.lease_seconds):
        return

    async def boundary():
        await asyncio.to_thread(store.boundary, task_id, owner)

    async def emit(kind, data):
        event = await asyncio.to_thread(store.emit, task_id, owner, kind, data)
        try:
            await bus.publish(f"shijian:events:{task_id}", str(event["event_id"]))
        except redis.RedisError:
            # Redis notifications are hints; committed MySQL events are replayed by WebSocket polling.
            log.warning("event_notification_unavailable task=%s", task_id)

    async def remember(e):
        await asyncio.to_thread(store.remember, task_id, owner, e)

    async def heartbeat():
        while True:
            await asyncio.sleep(config.heartbeat_seconds)
            if not await asyncio.to_thread(store.heartbeat, task_id, owner, config.lease_seconds):
                return

    heartbeat_job = asyncio.create_task(heartbeat())
    task = {"mode": "local"}
    model = None
    try:
        task = await asyncio.to_thread(store.get, task_id)
        if task["mode"] == "hybrid" and not config.search_api_key.get_secret_value():
            raise ValueError("联网模式缺少 SEARCH_API_KEY")
        model = model_factory(config)
        agent = ResearchAgent(config, model, corpus, emit, boundary, remember)
        result = await agent.run(task["question"], task["mode"])
        await boundary()
        await asyncio.to_thread(store.finish, task_id, owner, "completed", result)
    except Cancelled:
        await asyncio.to_thread(store.finish, task_id, owner, "cancelled")
    except Exception as exc:
        # Task boundary is intentionally broad: isolate one task, never pretend it completed.
        # No remote message/traceback is logged: SDK exceptions can include request credentials.
        http_status = getattr(exc, "status_code", None)
        log.error(
            "task_failed task=%s error_type=%s http_status=%s", task_id, type(exc).__name__, http_status
        )
        message = (
            (
                "模型服务返回 HTTP 402：账户余额不足，请补充余额后新建研究任务。"
                if http_status == 402
                else None
            )
            or config.model_error()
            or (
                "联网模式缺少 SEARCH_API_KEY"
                if task["mode"] == "hybrid" and not config.search_api_key.get_secret_value()
                else f"研究失败（{type(exc).__name__}）；请检查服务连接和模型配置，修复后新建任务"
            )
        )
        await asyncio.to_thread(store.finish, task_id, owner, "failed", None, message)
    finally:
        heartbeat_job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_job
        if model is not None:
            await model.aclose()


async def run_worker():
    config = settings()
    store = TaskStore(sessions())
    corpus = Corpus(sessions(), Vectors(config))
    owner = str(uuid4())
    bus = redis.from_url(config.redis_url, decode_responses=True, socket_timeout=10)
    try:
        while True:
            await asyncio.to_thread(store.reap)
            try:
                # Repair a crash between MySQL commit and Redis enqueue, including lost Redis data.
                for task_id in await asyncio.to_thread(store.pending):
                    await enqueue(bus, task_id)
                item = await bus.brpop(QUEUE, timeout=3)
                if item:
                    await bus.delete(f"shijian:queued:{item[1]}")
                    await execute_task(item[1], owner, config, store, bus, corpus)
            except redis.RedisError:
                log.warning("queue_unavailable; retry_in_seconds=3")
                await asyncio.sleep(3)
    finally:
        await bus.aclose()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
