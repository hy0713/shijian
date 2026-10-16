import threading

import fakeredis.aioredis
import redis.exceptions
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from shijian import api
from shijian.config import Settings
from shijian.tasks import TaskStore
from shijian.worker import execute_task
from tests.fakes import ScriptedModel


async def test_worker_failure_and_cancellation(sf, corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    store = TaskStore(sf)
    bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
    config = Settings(_env_file=None)
    tid = store.create("测试失败", "local")
    await execute_task(tid, "w", config, store, bus, corpus, lambda _: ScriptedModel(fail=True))
    task = store.get(tid)
    assert task["status"] == "failed" and "fake-secret-key" not in str(task)
    tid = store.create("测试取消", "local")

    async def cancel_then_choose(model, messages, tools):
        store.cancel(tid)
        return await ScriptedModel.choose(model, messages, tools)

    model = ScriptedModel()
    model.choose = lambda messages, tools: cancel_then_choose(model, messages, tools)
    await execute_task(tid, "w", config, store, bus, corpus, lambda _: model)
    assert store.get(tid)["status"] == "cancelled"
    await bus.aclose()


async def test_model_balance_failure_is_actionable_and_not_completed(sf, corpus):
    store = TaskStore(sf)
    bus = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class BalanceError(Exception):
        status_code = 402

    class NoBalanceModel(ScriptedModel):
        async def choose(self, messages, tools):
            raise BalanceError("TEST-ONLY secret exception must not be exposed")

    tid = store.create("余额错误状态测试", "local")
    await execute_task(
        tid, "balance-test", Settings(_env_file=None), store, bus, corpus, lambda _: NoBalanceModel()
    )
    task = store.get(tid)
    assert task["status"] == "failed" and task["result"] is None
    assert "HTTP 402" in task["error"] and "余额不足" in task["error"]
    assert "secret" not in task["error"]
    await bus.aclose()


def test_websocket_disconnect_does_not_stop_worker_and_replays(monkeypatch, sf, corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    store = TaskStore(sf)
    gate = threading.Event()
    tid = store.create("浏览器断开测试", "local")
    monkeypatch.setattr(api, "store", lambda: store)
    with TestClient(api.app) as client:
        bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
        client.app.state.bus = bus

        async def run():
            await execute_task(
                tid, "w", Settings(_env_file=None), store, bus, corpus, lambda _: ScriptedModel(gate=gate)
            )

        future = client.portal.start_task_soon(run)
        with client.websocket_connect(f"/ws/tasks/{tid}") as ws:
            first = ws.receive_json()
            assert first["task_id"] == tid
            cursor = first["event_id"]
        gate.set()
        future.result(timeout=15)
        assert store.get(tid)["status"] == "completed"
        with client.websocket_connect(f"/ws/tasks/{tid}?after={cursor}") as ws:
            replay = []
            while True:
                event = ws.receive_json()
                replay.append(event)
                if event["type"] == "snapshot":
                    break
            assert replay[-1]["data"]["status"] == "completed"
            assert all(e["event_id"] > cursor for e in replay if e["event_id"])
        evidence = client.get(f"/api/tasks/{tid}/evidence").json()
        assert evidence
        assert client.get(f"/api/tasks/{tid}/evidence/fake-id").status_code == 404
        assert client.get("/api/tasks/missing").status_code == 404


def test_missing_config_and_import_formats(monkeypatch, sf, corpus):
    monkeypatch.setattr(api, "sessions", lambda: sf)
    monkeypatch.setattr(api, "store", lambda: TaskStore(sf))
    monkeypatch.setattr(api, "corpus", lambda: corpus)
    monkeypatch.setattr(api, "settings", lambda: Settings(_env_file=None))
    with TestClient(api.app) as client:
        assert client.get("/").status_code == 200
        response = client.post("/api/tasks", json={"question": "测试问题", "mode": "local"})
        assert response.status_code == 503 and "DEEPSEEK" in response.json()["detail"]
        assert client.post("/api/sources/upload", files={"file": ("x.exe", b"x")}).status_code == 400
        for suffix in ("txt", "md"):
            response = client.post(
                '/api/sources/upload?metadata={"title":"TEST"}',
                files={"file": (f"test.{suffix}", ("史思明仅为接口测试文字。" * 4).encode())},
            )
            assert response.status_code == 201
        assert len(client.get("/api/sources").json()) == 1


def test_hybrid_missing_search_and_durable_pending_when_redis_down(monkeypatch, sf):
    store = TaskStore(sf)
    monkeypatch.setattr(api, "store", lambda: store)
    config = Settings(
        _env_file=None,
        deepseek_api_key="TEST-ONLY",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="TEST-ONLY",
    )
    monkeypatch.setattr(api, "settings", lambda: config)

    class DownBus:
        async def set(self, *args, **kwargs):
            raise redis.exceptions.ConnectionError()

        async def aclose(self):
            pass

    with TestClient(api.app) as client:
        response = client.post("/api/tasks", json={"question": "测试联网配置", "mode": "hybrid"})
        assert response.status_code == 503 and "SEARCH_API_KEY" in response.json()["detail"]
        client.app.state.bus = DownBus()
        response = client.post("/api/tasks", json={"question": "测试持久化补投", "mode": "local"})
        assert response.status_code == 201 and response.json()["queued"] is False
        tid = response.json()["task_id"]
        assert store.get(tid)["status"] == "pending" and tid in store.pending()
        assert "TEST-ONLY" not in response.text


def test_database_outage_is_explicit_without_remote_exception_text(monkeypatch):
    def broken_sessions():
        raise OperationalError("TEST", {}, Exception("test-secret-must-not-appear"))

    monkeypatch.setattr(api, "sessions", broken_sessions)
    with TestClient(api.app) as client:
        response = client.get("/api/sources")
        assert response.status_code == 503 and "MySQL" in response.json()["detail"]
        assert "test-secret-must-not-appear" not in response.text
