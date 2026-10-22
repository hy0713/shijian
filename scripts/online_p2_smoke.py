"""Exercise collection/index reuse against localhost, without creating research/model requests."""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--task-id", required=True, help="An existing task; it will not be re-run")
    parser.add_argument("--collection-id", required=True)
    parser.add_argument("--query", default="辛丑条约")
    parser.add_argument("--output", type=Path, default=Path("docs/online-p2-smoke.json"))
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, trust_env=False, timeout=30) as client:

        def call(method, path, **kwargs):
            response = client.request(method, "/api/" + path, **kwargs)
            response.raise_for_status()
            return response.json()

        sid, tid, cid = args.source_id, args.task_id, args.collection_id
        sources_before = call("GET", "sources?limit=100")
        tasks_before = call("GET", "tasks?limit=100")
        assert not [t for t in tasks_before if t["status"] in {"pending", "running"}]
        before = call("GET", "sources/" + sid)
        task_before = call("GET", "tasks/" + tid)
        evidence_before = call("GET", f"tasks/{tid}/evidence")
        assert before["saved"] and before["index_status"] == "ready"
        try:
            call("PUT", f"sources/{sid}/bookmark", json={"saved": False})
            excluded = call("POST", "corpus/search?query=" + args.query, json={"source_ids": [sid]})
            assert not excluded
            assert call("GET", f"tasks/{tid}/evidence") == evidence_before
        finally:
            call("PUT", f"sources/{sid}/bookmark", json={"saved": True})
        requested = call("POST", f"sources/{sid}/index")
        states = [requested["index_status"]]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            after = call("GET", "sources/" + sid)
            if after["index_status"] != states[-1]:
                states.append(after["index_status"])
            if after["index_status"] in {"ready", "failed"}:
                break
            time.sleep(0.5)
        assert after["index_status"] == "ready", "Index did not complete; original remains preserved"
        hits = call("POST", "corpus/search?query=" + args.query, json={"source_ids": [sid]})
        assert hits and {h["source_id"] for h in hits} == {sid}
        for _ in range(2):
            call("PUT", f"collections/{cid}/sources/{sid}")
            call("PUT", f"collections/{cid}/tasks/{tid}")
        detail = call("GET", "collections/" + cid)
        assert sum(s["source_id"] == sid for s in detail["sources"]) == 1
        assert sum(t["task_id"] == tid for t in detail["tasks"]) == 1
        assert before["raw_text"] == after["raw_text"] and before["outline"] == after["outline"]
        assert call("GET", "tasks/" + tid) == task_before
        assert call("GET", f"tasks/{tid}/evidence") == evidence_before
        sources_after = call("GET", "sources?limit=100")
        assert {r["source_id"]: r["content_hash"] for r in sources_before} == {
            r["source_id"]: r["content_hash"] for r in sources_after
        }
        assert call("GET", "tasks?limit=100") == tasks_before
        record = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "source_id": sid,
            "task_id": tid,
            "collection_id": cid,
            "source_count": len(sources_after),
            "tasks_first_page": len(tasks_before),
            "observed_index_states": states,
            "indexed_passages": after["indexed_passages"],
            "scoped_retrieval_hits": len(hits),
            "original_sha256": hashlib.sha256(after["raw_text"].encode()).hexdigest(),
            "checks": {
                "unsaved_source_excluded": True,
                "source_ids_and_hashes_preserved": True,
                "outline_and_original_unchanged": True,
                "evidence_and_task_unchanged": True,
                "duplicate_membership_idempotent": True,
                "real_background_index_ready": True,
                "no_new_research_tasks": True,
            },
            "paid_requests": {"model": 0, "search": 0, "fetch": 0},
            "scope": "Existing real source and task; this checks infrastructure, not answer quality.",
        }
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(json.dumps({"output": str(args.output), "checks": record["checks"], "index_states": states}))


if __name__ == "__main__":
    main()
