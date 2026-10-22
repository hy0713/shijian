import asyncio
import json
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import redis.asyncio as redis
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from opencc import OpenCC
from pydantic import BaseModel, HttpUrl, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import defer

from shijian.config import settings
from shijian.corpus import Corpus, digest
from shijian.db import (
    Collection,
    CollectionSource,
    CollectionTask,
    Source,
    SourceLocation,
    Task,
    engine,
    sessions,
)
from shijian.evaluations import router as evaluation_router
from shijian.export import markdown
from shijian.indexing import IndexStore, enqueue_index
from shijian.schema import CollectionInput, DocumentInput, ResearchInput, SearchFilter
from shijian.source_reading import page_outline
from shijian.tasks import TERMINAL, TaskStore, enqueue, task_dict
from shijian.vectors import Vectors
from shijian.web import FetchError, canonical_url, fetch_page

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    app.state.bus = redis.from_url(settings().redis_url, decode_responses=True, socket_timeout=5)
    yield
    await app.state.bus.aclose()


app = FastAPI(title="史鉴", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(evaluation_router)


def store():
    return TaskStore(sessions())


@lru_cache
def corpus():
    return Corpus(sessions(), Vectors(settings()))


@app.exception_handler(LookupError)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(FetchError)
async def fetch_error(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(OperationalError)
async def database_unavailable(request, exc):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "数据库暂不可用，请检查 MySQL 是否已启动、数据库迁移和连接配置；没有使用临时数据库替代。"
        },
    )


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    with engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/config")
def config_status():
    config = settings()
    return {
        "model_ready": not config.model_error(),
        "model_error": config.model_error(),
        "web_ready": config.web_ready(),
        "search_provider": config.search_provider,
        "web_error": config.web_error(),
        "web_limits": {"searches": config.max_web_searches, "pages": config.max_web_pages},
        "notice": "仅检查配置是否填写，未验证凭据有效性。全网模式不使用预装资料；限定资料模式也需聊天模型，Embedding独立配置。",
    }


@app.post("/api/tasks", status_code=201)
async def create_task(body: ResearchInput, request: Request):
    config = settings()
    if error := config.model_error():
        raise HTTPException(503, error)
    if body.mode in {"online", "hybrid"} and (error := config.web_error()):
        raise HTTPException(503, error + "；请配置联网工具或选择限定资料研究")
    try:
        task_id = await asyncio.to_thread(
            store().create,
            body.question,
            body.mode,
            None if body.source_ids is None else [str(x) for x in body.source_ids],
            str(body.collection_id) if body.collection_id else None,
            corpus().vectors.key,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    queued = True
    try:
        await enqueue(request.app.state.bus, task_id)
    except redis.RedisError:
        queued = False  # Durable pending task is repaired by the worker on Redis recovery.
    return {
        "task_id": task_id,
        "status": "pending",
        "queued": queued,
        "notice": None if queued else "任务已持久化，Redis 恢复后 worker 将补投队列",
    }


@app.get("/api/tasks")
def history(
    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), query: str = Query("", max_length=200)
):
    return store().history(limit, offset, query)


@app.get("/api/tasks/{task_id}")
def task(task_id: str):
    return store().get(task_id)


@app.get("/api/tasks/{task_id}/result")
def result(task_id: str):
    task = store().get(task_id)
    return {"task_id": task_id, "status": task["status"], "result": task["result"], "error": task["error"]}


@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str):
    return store().cancel(task_id)


@app.get("/api/tasks/{task_id}/export")
def export_task(task_id: str):
    task = store().get(task_id)
    evidence = {e["evidence_id"]: e for e in store().evidence(task_id)}
    try:
        body = markdown(task, evidence)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, "保存的回答与证据快照不一致，导出已停止") from exc
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="shijian-{task_id}.md"'},
    )


@app.get("/api/tasks/{task_id}/events")
def events(task_id: str, after: int = Query(0, ge=0)):
    store().get(task_id)
    return store().events(task_id, after)


@app.get("/api/tasks/{task_id}/evidence")
def evidence(task_id: str):
    store().get(task_id)
    return store().evidence(task_id)


@app.get("/api/tasks/{task_id}/evidence/{evidence_id}")
def evidence_detail(task_id: str, evidence_id: str):
    return store().evidence(task_id, evidence_id)


@app.get("/api/passages/{passage_id}")
def passage(passage_id: str):
    return corpus().read(passage_id)


def source_summary(x):
    keys = (
        "id",
        "title",
        "chapter",
        "author",
        "source_type",
        "source_url",
        "edition",
        "index_status",
        "people",
        "topics",
        "content_hash",
        "indexed_passages",
        "index_total",
        "index_error",
        "saved",
    )
    return {("source_id" if k == "id" else k): getattr(x, k) for k in keys} | {
        "imported_at": x.imported_at.isoformat() + "Z"
    }


@app.get("/api/sources")
def sources(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    query: str = Query("", max_length=200),
    source_type: str = Query("", max_length=30),
    index_status: str = Query("", max_length=20),
    person: str = Query("", max_length=100),
    topic: str = Query("", max_length=100),
    saved: bool | None = None,
):
    normalize = OpenCC("t2s").convert
    with sessions()() as s:
        # Metadata only; originals are fetched on demand by the reader. Filter before pagination.
        rows = s.scalars(select(Source).options(defer(Source.raw_text)).order_by(Source.imported_at.desc()))
        selected = [
            x
            for x in rows
            if (
                not query
                or normalize(query).casefold()
                in normalize(" ".join(v or "" for v in (x.title, x.chapter, x.author))).casefold()
            )
            and (not source_type or x.source_type == source_type)
            and (not index_status or x.index_status == index_status)
            and (not person or normalize(person) in [normalize(v) for v in x.people])
            and (not topic or normalize(topic) in [normalize(v) for v in x.topics])
            and (saved is None or x.saved == saved)
        ]
        return [source_summary(x) for x in selected[offset : offset + limit]]


async def schedule_source(source_id, request, force=False):
    result = await asyncio.to_thread(IndexStore(sessions()).request, source_id, force)
    if result["index_status"] == "pending":
        try:
            await enqueue_index(request.app.state.bus, source_id)
            result["queued"] = True
        except redis.RedisError:
            result["queued"] = False
    return result


@app.post("/api/sources/{source_id}/index", status_code=202)
async def request_index(source_id: str, request: Request):
    try:
        return await schedule_source(source_id, request, force=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class BookmarkInput(BaseModel):
    saved: bool


@app.put("/api/sources/{source_id}/bookmark")
async def bookmark_source(source_id: str, body: BookmarkInput, request: Request):
    with sessions().begin() as s:
        row = s.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if row is None:
            raise LookupError("资料不存在")
        row.saved = body.saved
        if not body.saved and row.index_status == "pending":
            row.index_status = "unrequested"
    state = await schedule_source(source_id, request) if body.saved else {"source_id": source_id}
    return state | {"saved": body.saved}


def collection_row(s, collection_id):
    row = s.scalar(select(Collection).where(Collection.id == collection_id).with_for_update())
    if row is None:
        raise LookupError("专题不存在")
    return row


def collection_summary(s, row):
    counts = {}
    for model, key in ((CollectionSource, "source_count"), (CollectionTask, "task_count")):
        counts[key] = s.scalar(select(func.count()).select_from(model).where(model.collection_id == row.id))
    return {col.name: getattr(row, col.name) for col in Collection.__table__.columns} | counts


@app.get("/api/collections")
def collections(
    query: str = Query("", max_length=100),
    person: str = Query("", max_length=100),
    event: str = Query("", max_length=100),
    region: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
):
    convert = OpenCC("t2s").convert

    def normalize(value):
        return convert(value).casefold()

    with sessions()() as s:
        rows = [
            r
            for r in s.scalars(select(Collection).order_by(Collection.created_at.desc()))
            if (not query or normalize(query) in normalize(r.name + " " + r.description))
            and all(
                not value or normalize(value) in [normalize(tag) for tag in getattr(r, field)]
                for field, value in (("people", person), ("events", event), ("regions", region))
            )
        ]
        return [collection_summary(s, r) for r in rows[offset : offset + limit]]


@app.post("/api/collections", status_code=201)
def create_collection(body: CollectionInput):
    with sessions().begin() as s:
        row = Collection(id=str(uuid4()), **body.model_dump())
        s.add(row)
        s.flush()
        return collection_summary(s, row)


@app.get("/api/collections/{collection_id}")
def collection_detail(
    collection_id: str,
    source_offset: int = Query(0, ge=0),
    task_offset: int = Query(0, ge=0),
):
    with sessions()() as s:
        row = s.get(Collection, collection_id)
        if row is None:
            raise LookupError("专题不存在")
        docs = list(
            s.scalars(
                select(Source)
                .join(CollectionSource)
                .options(defer(Source.raw_text))
                .where(CollectionSource.collection_id == collection_id)
                .order_by(Source.imported_at.desc())
                .offset(source_offset)
                .limit(30)
            )
        )
        tasks = list(
            s.scalars(
                select(Task)
                .join(CollectionTask)
                .options(defer(Task.result))
                .where(CollectionTask.collection_id == collection_id)
                .order_by(Task.created_at.desc())
                .offset(task_offset)
                .limit(30)
            )
        )
        return collection_summary(s, row) | {
            "sources": [source_summary(r) for r in docs],
            "tasks": [task_dict(t, include_result=False) for t in tasks],
        }


@app.put("/api/collections/{collection_id}")
def edit_collection(collection_id: str, body: CollectionInput):
    with sessions().begin() as s:
        row = collection_row(s, collection_id)
        for key, value in body.model_dump().items():
            setattr(row, key, value)
        return collection_summary(s, row)


@app.delete("/api/collections/{collection_id}")
def remove_collection(collection_id: str):
    with sessions().begin() as s:
        row = collection_row(s, collection_id)
        s.execute(delete(CollectionTask).where(CollectionTask.collection_id == collection_id))
        s.execute(delete(CollectionSource).where(CollectionSource.collection_id == collection_id))
        s.delete(row)
    return {"removed": True, "notice": "仅移除专题及关联；文献、研究记录与引用均保留"}


@app.put("/api/collections/{collection_id}/sources/{source_id}")
async def collect_source(collection_id: str, source_id: str, request: Request):
    with sessions().begin() as s:
        collection_row(s, collection_id)  # Serialize duplicate membership writes on the parent row.
        source = s.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if source is None:
            raise LookupError("资料不存在")
        source.saved = True
        if s.get(CollectionSource, (collection_id, source_id)) is None:
            s.add(CollectionSource(collection_id=collection_id, source_id=source_id))
    return await schedule_source(source_id, request) | {"saved": True, "collection_id": collection_id}


@app.put("/api/collections/{collection_id}/tasks/{task_id}")
def collect_task(collection_id: str, task_id: str):
    with sessions().begin() as s:
        collection_row(s, collection_id)
        if s.get(Task, task_id) is None:
            raise LookupError("任务不存在")
        if s.get(CollectionTask, (collection_id, task_id)) is None:
            s.add(CollectionTask(collection_id=collection_id, task_id=task_id))
    return {"collection_id": collection_id, "task_id": task_id}


@app.delete("/api/collections/{collection_id}/sources/{source_id}")
def uncollect_source(collection_id: str, source_id: str):
    with sessions().begin() as s:
        collection_row(s, collection_id)
        s.execute(
            delete(CollectionSource).where(
                CollectionSource.collection_id == collection_id, CollectionSource.source_id == source_id
            )
        )
    return {"removed": True, "notice": "仅移出专题，收藏与原文保留"}


@app.delete("/api/collections/{collection_id}/tasks/{task_id}")
def uncollect_task(collection_id: str, task_id: str):
    with sessions().begin() as s:
        collection_row(s, collection_id)
        s.execute(
            delete(CollectionTask).where(
                CollectionTask.collection_id == collection_id, CollectionTask.task_id == task_id
            )
        )
    return {"removed": True, "notice": "仅移出专题，研究记录与引用保留"}


class MetadataPatch(BaseModel):
    model_config = {"extra": "forbid"}
    title: str | None = None
    author: str | None = None
    chapter: str | None = None
    edition: str | None = None
    source_type: str | None = None
    people: list[str] | None = None
    topics: list[str] | None = None
    provenance_note: str | None = None


@app.patch("/api/sources/{source_id}")
def edit_source(source_id: str, body: MetadataPatch):
    with sessions().begin() as s:
        source = s.get(Source, source_id)
        if source is None:
            raise LookupError("资料不存在")
        changes = body.model_dump(exclude_unset=True)
        try:
            DocumentInput.model_validate(
                {**{k: getattr(source, k) for k in DocumentInput.model_fields}, **changes}
            )
        except ValidationError as exc:
            raise HTTPException(422, "资料元数据不符合约定；标题、类型、人物和主题不能为空值") from exc
        for k, v in changes.items():
            setattr(source, k, v)
        return source_summary(source)


@app.get("/api/sources/{source_id}")
def source_detail(source_id: str):
    with sessions()() as s:
        source = s.get(Source, source_id)
        if source is None:
            raise LookupError("资料不存在")
        return {col.name: getattr(source, col.name) for col in Source.__table__.columns} | {
            "outline": page_outline(corpus().page_passages(source_id))[:80],
            "locations": list(
                s.scalars(select(SourceLocation.url).where(SourceLocation.source_id == source_id))
            ),
        }


@app.post("/api/corpus/search")
def search(query: str = Query(min_length=1, max_length=1000), filters: SearchFilter | None = None):
    return corpus().search(query, filters)


@app.post("/api/sources", status_code=201)
async def import_document(body: DocumentInput, request: Request):
    result = await asyncio.to_thread(corpus().import_document, body, False)
    return result | await schedule_source(result["source_id"], request)


@app.post("/api/sources/upload", status_code=201)
async def upload(request: Request, file: UploadFile = File(...), metadata: str = "{}"):
    if not file.filename or Path(file.filename).suffix.lower() not in {".txt", ".md", ".json"}:
        raise HTTPException(400, "仅支持 UTF-8 TXT、Markdown 或结构化 JSON")
    content = await file.read(2_000_001)
    await file.close()
    if len(content) > 2_000_000:
        raise HTTPException(413, "导入文件超过 2MB")
    try:
        decoded = content.decode("utf-8-sig")
        docs = parse_documents(decoded, Path(file.filename).suffix, json.loads(metadata))
    except (ValueError, UnicodeError, ValidationError) as exc:
        raise HTTPException(422, "文件或元数据格式无效，请检查 UTF-8 编码和导入格式") from exc
    results = await asyncio.to_thread(lambda: [corpus().import_document(doc, index=False) for doc in docs])
    return [r | await schedule_source(r["source_id"], request) for r in results]


def parse_documents(content, suffix, metadata):
    if suffix.lower() == ".json":
        payload = json.loads(content)
        rows = payload if isinstance(payload, list) else [payload]
        if len(rows) > 50:
            raise ValueError("单次最多导入 50 份资料")
        return [DocumentInput.model_validate(row) for row in rows]
    return [DocumentInput.model_validate({**metadata, "raw_text": content})]


class PageInput(BaseModel):
    url: HttpUrl
    metadata: MetadataPatch | None = None


@app.post("/api/sources/page", status_code=201)
async def import_page(body: PageInput, request: Request):
    url = canonical_url(str(body.url))
    parsed = urlsplit(url)
    revision = parse_qs(parsed.query).get("oldid", [""])[0]
    # A fixed revision is immutable. Mutable/latest URLs must be fetched again.
    if parsed.hostname == "zh.wikisource.org" and revision.isdecimal() and body.metadata is None:
        with sessions()() as s:
            existing = s.scalar(
                select(SourceLocation.source_id).where(SourceLocation.url_hash == digest(url))
            )
        if existing:
            return {
                "source_id": existing,
                "duplicate": True,
                "cached_revision": True,
            } | await bookmark_source(existing, BookmarkInput(saved=True), request)
    page = await fetch_page(str(body.url), settings())
    try:
        doc = DocumentInput(
            **(
                {
                    "title": page["title"],
                    "source_type": "网页",
                    "source_url": page["url"],
                    "raw_text": page["text"],
                    "edition": page.get("edition"),
                    "provenance_note": page.get(
                        "provenance_note", "网页正文快照，来源独立性与内容未经人工核查。"
                    ),
                }
                | (body.metadata.model_dump(exclude_unset=True) if body.metadata else {})
            )
        )
    except ValidationError as exc:
        raise HTTPException(422, "网页元数据无效，请检查标题和资料类型") from exc
    result = await asyncio.to_thread(corpus().import_document, doc, False)
    return result | await schedule_source(result["source_id"], request)


@app.websocket("/ws/tasks/{task_id}")
async def websocket_task(ws: WebSocket, task_id: str, after: int = Query(0, ge=0)):
    await ws.accept()
    try:
        await asyncio.to_thread(store().get, task_id)
    except LookupError:
        await ws.close(code=4404, reason="任务不存在")
        return
    # Subscribe first, then replay durable events: no replay/subscribe race. Redis loss falls back to polling.
    pubsub = ws.app.state.bus.pubsub()
    subscribed = False
    try:
        try:
            await pubsub.subscribe(f"shijian:events:{task_id}")
            subscribed = True
        except redis.RedisError:
            pass
        while True:
            batch = await asyncio.to_thread(store().events, task_id, after)
            for event in batch:
                await ws.send_json(event)
                after = event["event_id"]
            if len(batch) == 200:
                continue
            task = await asyncio.to_thread(store().get, task_id)
            if task["status"] in TERMINAL:
                # Race with completion is harmless: replay once more before closing.
                for event in await asyncio.to_thread(store().events, task_id, after):
                    await ws.send_json(event)
                await ws.send_json(
                    {
                        "event_id": None,
                        "task_id": task_id,
                        "type": "snapshot",
                        "time": task["updated_at"],
                        "data": task,
                    }
                )
                await ws.close()
                return
            if subscribed:
                try:
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                except redis.RedisError:
                    subscribed = False
            else:
                await asyncio.sleep(1)
            # Receive briefly so disconnected clients are released even when a task is quiet.
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=0.05)
                if message["type"] == "websocket.disconnect":
                    return
            except TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.aclose()


@app.get("/api/books/search")
async def discover_books(query: str = Query(min_length=1, max_length=200)):
    from shijian.books import search_books

    return {
        "provider": "wikisource",
        "results": await search_books(query, settings()),
        "notice": "检索摘要只是候选线索，导入正文并完成索引后才参与本地检索。",
    }
