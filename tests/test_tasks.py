from datetime import timedelta

import pytest

from shijian.db import Task, utcnow
from shijian.tasks import Cancelled, TaskStore


def test_atomic_claim_duplicate_consume_and_terminal(sf):
    store = TaskStore(sf)
    tid = store.create("测试提问", "local")
    assert store.claim(tid, "worker-1", 60)
    assert not store.claim(tid, "worker-2", 60)
    assert not store.finish(tid, "worker-2", "completed", {})
    assert store.finish(tid, "worker-1", "completed", {"test": True})
    assert not store.claim(tid, "worker-2", 60)
    assert store.get(tid)["result"] == {"test": True}
    events = store.events(tid)
    assert [e["data"]["status"] for e in events] == ["pending", "running", "completed"]
    assert store.events(tid, events[0]["event_id"]) == events[1:]


def test_cancel_at_pending_and_running_boundary(sf):
    store = TaskStore(sf)
    tid = store.create("测试提问", "local")
    assert store.cancel(tid)["status"] == "cancelled"
    assert not store.claim(tid, "worker", 60)
    tid = store.create("另一测试提问", "local")
    store.claim(tid, "worker", 60)
    store.cancel(tid)
    with pytest.raises(Cancelled):
        store.boundary(tid, "worker")
    store.finish(tid, "worker", "completed", {"should_not_publish": True})
    assert store.get(tid)["status"] == "cancelled"
    assert store.get(tid)["result"] is None


def test_orphan_recovery_and_ownership_fencing(sf):
    store = TaskStore(sf)
    tid = store.create("测试提问", "local")
    store.claim(tid, "dead", 60)
    with sf.begin() as s:
        s.get(Task, tid).lease_until = utcnow() - timedelta(seconds=1)
    assert store.reap() == 1
    assert store.get(tid)["status"] == "failed"
    assert not store.finish(tid, "dead", "completed", {})
    assert not store.heartbeat(tid, "dead", 60)
    with pytest.raises(Cancelled):
        store.emit(tid, "dead", "step", {})
