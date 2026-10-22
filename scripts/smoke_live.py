"""Live API/worker/WebSocket smoke test; really calls the configured model (not a test double)."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect


async def wait_task(client, task_id, timeout=300, wanted=None):
    deadline, previous = time.monotonic() + timeout, None
    usage_cursor = 0
    while time.monotonic() < deadline:
        response = await client.get(f"/api/tasks/{task_id}")
        response.raise_for_status()
        task = response.json()
        events = (await client.get(f"/api/tasks/{task_id}/events?after={usage_cursor}")).json()
        for event in events:
            usage_cursor = max(usage_cursor, event["event_id"])
            if event["type"] in {"model_usage", "external_usage"}:
                print(event["type"], json.dumps(event["data"], ensure_ascii=False), flush=True)
        if task["status"] != previous:
            print(task_id, task["status"], flush=True)
            previous = task["status"]
        if task["status"] in (wanted or {"completed", "failed", "cancelled"}):
            return task
        await asyncio.sleep(1)
    raise TimeoutError("Live task did not reach the expected state")


async def main(args):
    ws_base = args.base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=15, trust_env=False) as client:
        health = await client.get("/health")
        health.raise_for_status()
        sources = (await client.get("/api/sources")).json()
        if args.mode == "local":
            assert sources and all(s["index_status"] == "ready" for s in sources)
        created = await client.post("/api/tasks", json={"question": args.question, "mode": args.mode})
        created.raise_for_status()
        task_id = created.json()["task_id"]
        print("created", task_id, flush=True)
        received = []
        async with connect(f"{ws_base}/ws/tasks/{task_id}", proxy=None) as socket:
            for _ in range(3):
                event = json.loads(await asyncio.wait_for(socket.recv(), 45))
                received.append(event)
        cursor = max(e["event_id"] or 0 for e in received)
        print("websocket deliberately disconnected after cursor", cursor, flush=True)
        task = await wait_task(client, task_id)
        evidence = (await client.get(f"/api/tasks/{task_id}/evidence")).json()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "task": task,
                    "evidence": evidence,
                    "events": (await client.get(f"/api/tasks/{task_id}/events")).json(),
                    "checks": {"verification": "not_completed"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        if task["status"] != "completed":
            raise RuntimeError(f"Live research {task['status']}: {task['error']}")
        result = task["result"]
        by_id = {e["evidence_id"]: e for e in evidence}
        assert result["citations"], "Real research returned no checked citations"
        assert result["conclusions"], "Real research returned no supported core conclusions"
        for ref in result["citations"]:
            e = by_id[ref["evidence_id"]]
            assert ref["quote"] in e["text"] and ref["url"] == e["url"]
            original = (await client.get(f"/api/sources/{e['source_id']}")).json()
            assert original["raw_text"][ref["start"] : ref["end"]] == ref["quote"]
        replay = []
        async with connect(f"{ws_base}/ws/tasks/{task_id}?after={cursor}", proxy=None) as socket:
            async for message in socket:
                event = json.loads(message)
                replay.append(event)
                if event["type"] == "snapshot":
                    assert event["data"]["status"] == "completed"
                    break
        assert replay and all(e["event_id"] > cursor for e in replay if e["event_id"])
        usage_events = [e["data"] for e in received + replay if e["type"] == "model_usage"]
        assert usage_events == result["metrics"]["model_requests"]
        external_events = [e["data"] for e in received + replay if e["type"] == "external_usage"]
        assert [{k: v for k, v in row.items() if k != "summary"} for row in external_events] == result[
            "metrics"
        ].get("external_requests", [])
        if args.mode == "online":
            tool_names = [e["data"]["name"] for e in received + replay if e["type"] == "tool"]
            assert (
                "search_corpus" not in tool_names
                and "search_web" in tool_names
                and "fetch_page" in tool_names
            )
            assert all(e.get("retrieved_at") for e in evidence)
        print("checked citations", len(result["citations"]), "replayed events", len(replay), flush=True)
        cancellation = None
        if not args.skip_cancel:
            created = await client.post(
                "/api/tasks",
                json={"question": "请对照两唐书核查史思明与安庆绪的关系和先后时序。", "mode": "local"},
            )
            created.raise_for_status()
            cancel_id = created.json()["task_id"]
            running = await wait_task(
                client, cancel_id, timeout=120, wanted={"running", "failed", "completed"}
            )
            assert running["status"] == "running"
            response = await client.post(f"/api/tasks/{cancel_id}/cancel")
            response.raise_for_status()
            cancellation = await wait_task(client, cancel_id, timeout=60)
            assert cancellation["status"] == "cancelled" and cancellation["result"] is None
        report = {
            "task": task,
            "evidence": evidence,
            "initial_events": received,
            "replayed_events": replay,
            "checks": {
                "real_model": True,
                "mysql_milvus_sources": len(sources),
                "citations_checked": len(result["citations"]),
                "websocket_disconnect_did_not_cancel": True,
                "websocket_replay": True,
                "model_usage_events_replayed": len(usage_events),
                "external_usage_events_replayed": len(external_events),
                "mode": args.mode,
                "running_task_cancelled": cancellation is not None,
            },
            "cancelled_task_id": cancellation["task_id"] if cancellation else None,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print("PASS", args.output, json.dumps(result["metrics"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--question", default="为什么安禄山和史思明会翻脸？")
    parser.add_argument("--mode", choices=["local", "hybrid", "online"], default="local")
    parser.add_argument("--output", type=Path, default=Path(".cache/live-smoke.json"))
    parser.add_argument("--skip-cancel", action="store_true")
    asyncio.run(main(parser.parse_args()))
