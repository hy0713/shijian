import asyncio
import json
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import redis.asyncio as redis
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import Source, SourceLocation, engine, sessions
from shijian.schema import DocumentInput, ResearchInput, SearchFilter
from shijian.tasks import TERMINAL, TaskStore, enqueue
from shijian.vectors import Vectors
from shijian.web import FetchError, fetch_page

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    app.state.bus = redis.from_url(settings().redis_url, decode_responses=True, socket_timeout=5)
    yield
    await app.state.bus.aclose()


app = FastAPI(title="史鉴", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
        "web_ready": bool(config.search_api_key.get_secret_value()),
        "notice": "仅检查配置是否填写，未验证凭据有效性。本地模式也需 DeepSeek；Embedding 单独配置。",
    }


@app.post("/api/tasks", status_code=201)
async def create_task(body: ResearchInput, request: Request):
    config = settings()
    if error := config.model_error():
        raise HTTPException(503, error)
    if body.mode == "hybrid" and not config.search_api_key.get_secret_value():
        raise HTTPException(503, "联网模式缺少 SEARCH_API_KEY，请配置独立搜索服务或选择本地模式")
    task_id = await asyncio.to_thread(store().create, body.question, body.mode)
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
def history(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return store().history(limit, offset)


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


@app.get("/api/sources")
def sources(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    with sessions()() as s:
        return [
            {
                "source_id": x.id,
                "title": x.title,
                "chapter": x.chapter,
                "author": x.author,
                "source_type": x.source_type,
                "source_url": x.source_url,
                "edition": x.edition,
                "index_status": x.index_status,
                "people": x.people,
                "topics": x.topics,
                "content_hash": x.content_hash,
                "imported_at": x.imported_at.isoformat() + "Z",
            }
            for x in s.scalars(select(Source).order_by(Source.imported_at.desc()).offset(offset).limit(limit))
        ]


@app.get("/api/sources/{source_id}")
def source_detail(source_id: str):
    with sessions()() as s:
        source = s.get(Source, source_id)
        if source is None:
            raise LookupError("资料不存在")
        return {col.name: getattr(source, col.name) for col in Source.__table__.columns} | {
            "locations": list(
                s.scalars(select(SourceLocation.url).where(SourceLocation.source_id == source_id))
            )
        }


@app.post("/api/corpus/search")
def search(query: str = Query(min_length=1, max_length=1000), filters: SearchFilter | None = None):
    return corpus().search(query, filters)


@app.post("/api/sources", status_code=201)
def import_document(body: DocumentInput):
    # Import is metadata-only in HTTP to avoid blocking the single research worker on model download.
    return corpus().import_document(body, index=False)


@app.post("/api/sources/upload", status_code=201)
async def upload(file: UploadFile = File(...), metadata: str = "{}"):
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
    return await asyncio.to_thread(lambda: [corpus().import_document(doc, index=False) for doc in docs])


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


@app.post("/api/sources/page", status_code=201)
async def import_page(body: PageInput):
    page = await fetch_page(str(body.url), settings())
    doc = DocumentInput(
        title=page["title"], source_type="网页", source_url=page["url"], raw_text=page["text"]
    )
    return await asyncio.to_thread(corpus().import_document, doc, False)


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
