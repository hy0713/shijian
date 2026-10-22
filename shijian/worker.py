import asyncio
import contextlib
import logging
from uuid import uuid4

import redis.asyncio as redis

from shijian.agent import ResearchAgent
from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import sessions
from shijian.model import ModelBudgetExceeded, ModelOutputError, ResearchModel
from shijian.schema import SearchFilter
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
    agent = None
    scope_error = None
    try:
        task = await asyncio.to_thread(store.get, task_id)
        if task["mode"] in {"online", "hybrid"} and (error := config.web_error()):
            raise ValueError(error)
        scoped = (
            corpus
            if task["source_ids"] is None
            else Corpus(corpus.sessions, corpus.vectors, task["source_ids"])
        )
        if task["source_ids"] is not None and set(task["source_ids"]) != set(scoped.eligible(SearchFilter())):
            scope_error = "本次固定范围中的资料已取消收藏或索引未就绪；请恢复资料后重试，未调用模型。"
            raise ValueError(scope_error)
        model = model_factory(config)

        async def report_usage(record):
            await emit("model_usage", record)

        model.on_usage = report_usage
        agent = ResearchAgent(config, model, scoped, emit, boundary, remember)
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
        if model is not None:
            await emit(
                "diagnostic",
                {
                    "error_type": type(exc).__name__,
                    "structured_failures": getattr(model, "structured_failures", []),
                    "model_requests": getattr(model, "request_metrics", []),
                    "token_usage": model.usage(),
                    "external_requests": agent.external_requests if agent else [],
                    "web_usage": agent.web_usage() if agent else None,
                },
            )
        message = (
            (
                "已达到本任务的模型调用或已观测 Token 上限，已停止继续调用。已读证据保留；请缩小问题后再研究。"
                if isinstance(exc, ModelBudgetExceeded)
                else None
            )
            or (
                "外部服务返回 HTTP 402：账户余额不足，请补充余额后新建研究任务。"
                if http_status == 402
                else None
            )
            or (
                "外部服务返回 HTTP 401：密钥无效，请检查对应模型或搜索服务配置。"
                if http_status == 401
                else None
            )
            or (
                "模型输出未通过结构化校验，已用尽本任务的格式修正预算。请缩小问题后重新研究；已读证据保留。"
                if isinstance(exc, ModelOutputError)
                else None
            )
            or scope_error
            or config.model_error()
            or (
                config.web_error()
                if task["mode"] in {"online", "hybrid"} and not config.web_ready()
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
