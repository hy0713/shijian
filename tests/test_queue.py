import fakeredis.aioredis

from shijian.tasks import QUEUE, enqueue


async def test_queue_advisory_dedup_and_repair_after_lost_enqueue():
    bus = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await enqueue(bus, "test-task")
    await enqueue(bus, "test-task")
    assert await bus.llen(QUEUE) == 1
    assert await bus.rpop(QUEUE) == "test-task"
    # Simulate SET succeeding but LPUSH being lost; expiry makes durable pending repair possible.
    await bus.delete("shijian:queued:test-task")
    await enqueue(bus, "test-task")
    assert await bus.rpop(QUEUE) == "test-task"
    await bus.aclose()
