from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import defer

from shijian.db import Collection, CollectionSource, CollectionTask, Event, Source, Task, TaskEvidence, utcnow

TERMINAL = {"completed", "failed", "cancelled"}
QUEUE = "shijian:queue"


async def enqueue(bus, task_id):
    # Advisory dedup only. A crash between SET and LPUSH is repaired after this short TTL.
    if await bus.set(f"shijian:queued:{task_id}", "1", nx=True, ex=60):
        await bus.lpush(QUEUE, task_id)


def event_dict(event):
    return {
        "event_id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "time": event.time.isoformat() + "Z",
        "data": event.data,
    }


def task_dict(task, include_result=True):
    return {
        "task_id": task.id,
        "question": task.question,
        "mode": task.mode,
        "source_ids": task.source_ids,
        "status": task.status,
        "cancel_requested": task.cancel_requested,
        "created_at": task.created_at.isoformat() + "Z",
        "updated_at": task.updated_at.isoformat() + "Z",
        "error": task.error,
    } | ({"result": task.result} if include_result else {})


class Cancelled(Exception):
    pass


class TaskStore:
    def __init__(self, sessions):
        self.sessions = sessions

    def create(self, question, mode, source_ids=None, collection_id=None, embedding_key=None):
        task_id = str(uuid4())
        with self.sessions.begin() as s:
            if collection_id or source_ids is not None:
                if mode != "local":
                    raise ValueError("专题资料范围仅用于限定资料研究；全网研究可在结束后加入专题")
                if collection_id and source_ids is not None:
                    raise ValueError("不能同时指定专题和资料范围")
                if collection_id:
                    if not s.scalar(
                        select(Collection).where(Collection.id == collection_id).with_for_update()
                    ):
                        raise LookupError("专题不存在")
                    source_ids = list(
                        s.scalars(
                            select(CollectionSource.source_id).where(
                                CollectionSource.collection_id == collection_id
                            )
                        )
                    )
                source_ids = list(dict.fromkeys(source_ids))
                if not 1 <= len(source_ids) <= 30:
                    raise ValueError("限定研究须选择 1–30 份资料；空专题不能回退到全库")
                ready = list(
                    s.scalars(
                        select(Source.id).where(
                            Source.id.in_(source_ids),
                            Source.saved.is_(True),
                            Source.index_status == "ready",
                            Source.embedding_key == embedding_key,
                        )
                    )
                )
                if set(ready) != set(source_ids):
                    raise ValueError(
                        "所选资料尚未全部收藏并完成当前模型的索引；请查看索引状态或移出未就绪资料"
                    )
            s.add(Task(id=task_id, question=question, mode=mode, source_ids=source_ids))
            s.flush()
            if collection_id:
                s.add(CollectionTask(collection_id=collection_id, task_id=task_id))
            s.add(Event(task_id=task_id, type="status", data={"status": "pending"}))
        return task_id

    def get(self, task_id):
        with self.sessions() as s:
            task = s.get(Task, task_id)
            if task is None:
                raise LookupError("任务不存在")
            return task_dict(task)

    def history(self, limit=30, offset=0, query=""):
        with self.sessions() as s:
            return [
                task_dict(t, include_result=False)
                for t in s.scalars(
                    select(Task)
                    .options(defer(Task.result))
                    .where(Task.question.contains(query))
                    .order_by(Task.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ]

    def pending(self):
        with self.sessions() as s:
            return list(s.scalars(select(Task.id).where(Task.status == "pending").limit(100)))

    def claim(self, task_id, owner, lease_seconds):
        with self.sessions.begin() as s:
            won = s.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "pending", Task.cancel_requested.is_(False))
                .values(
                    status="running", owner=owner, lease_until=utcnow() + timedelta(seconds=lease_seconds)
                )
            ).rowcount
            if won:
                s.add(Event(task_id=task_id, type="status", data={"status": "running"}))
        return bool(won)

    def heartbeat(self, task_id, owner, lease_seconds):
        with self.sessions.begin() as s:
            return bool(
                s.execute(
                    update(Task)
                    .where(
                        Task.id == task_id,
                        Task.owner == owner,
                        Task.status == "running",
                        Task.lease_until >= utcnow(),
                    )
                    .values(lease_until=utcnow() + timedelta(seconds=lease_seconds))
                ).rowcount
            )

    def boundary(self, task_id, owner):
        with self.sessions() as s:
            t = s.get(Task, task_id)
            if t.status != "running" or t.owner != owner or t.cancel_requested or t.lease_until < utcnow():
                raise Cancelled("任务已取消或 worker 租约已失效")

    def emit(self, task_id, owner, event_type, data):
        with self.sessions.begin() as s:
            t = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if t.status != "running" or t.owner != owner:
                raise Cancelled("任务所有权已失效")
            e = Event(task_id=task_id, type=event_type, data=data)
            s.add(e)
            s.flush()
            return event_dict(e)

    def events(self, task_id, after=0, limit=200):
        with self.sessions() as s:
            return [
                event_dict(e)
                for e in s.scalars(
                    select(Event)
                    .where(Event.task_id == task_id, Event.id > after)
                    .order_by(Event.id)
                    .limit(limit)
                )
            ]

    def remember(self, task_id, owner, evidence):
        with self.sessions.begin() as s:
            t = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if t.status != "running" or t.owner != owner or t.cancel_requested:
                raise Cancelled()
            key = (task_id, evidence["evidence_id"])
            if not s.get(TaskEvidence, key):
                s.add(
                    TaskEvidence(
                        task_id=task_id,
                        evidence_id=evidence["evidence_id"],
                        passage_id=evidence["passage_id"],
                        snapshot=evidence,
                    )
                )

    def evidence(self, task_id, evidence_id=None):
        with self.sessions() as s:
            stmt = select(TaskEvidence).where(TaskEvidence.task_id == task_id)
            if evidence_id:
                row = s.scalar(stmt.where(TaskEvidence.evidence_id == evidence_id))
                if row is None:
                    raise LookupError("该任务未读取此证据")
                return row.snapshot
            return [row.snapshot for row in s.scalars(stmt)]

    def finish(self, task_id, owner, status, result=None, error=None):
        with self.sessions.begin() as s:
            task = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task.status != "running" or task.owner != owner:
                return False
            if task.cancel_requested:
                status, result, error = "cancelled", None, None
            task.status, task.result, task.error = status, result, error
            task.lease_until = None
            s.add(Event(task_id=task_id, type=status, data={"status": status, "error": error}))
        return True

    def cancel(self, task_id):
        with self.sessions.begin() as s:
            t = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if t is None:
                raise LookupError("任务不存在")
            if t.status not in TERMINAL:
                t.cancel_requested = True
                if t.status == "pending":
                    t.status = "cancelled"
                s.add(
                    Event(task_id=task_id, type="status", data={"status": t.status, "cancel_requested": True})
                )
            return {"status": t.status, "cancel_requested": t.cancel_requested}

    def reap(self):
        """No automatic paid research replay: orphaned running tasks fail, pending tasks are redispatched."""
        with self.sessions.begin() as s:
            rows = list(
                s.scalars(
                    select(Task)
                    .where(Task.status == "running", Task.lease_until < utcnow())
                    .with_for_update()
                )
            )
            for t in rows:
                t.status = "cancelled" if t.cancel_requested else "failed"
                t.error = None if t.cancel_requested else "worker 租约过期：可能异常退出，请创建新任务重试"
                t.lease_until = None
                s.add(Event(task_id=t.id, type=t.status, data={"status": t.status, "error": t.error}))
            return len(rows)
