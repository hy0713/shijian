"""Offline usage audit of saved evaluation records; never calls a model or estimates fees."""

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path


def audit():
    groups, seen = {}, set()

    def add(group, rows):
        counts = Counter()
        for row in rows:
            metrics = row.get("result", {}).get("metrics", row.get("metrics")) or {}
            usage = metrics.get("token_usage", {})
            signature = json.dumps(
                [row.get("question_id"), row.get("method"), usage, metrics.get("model_requests")],
                sort_keys=True,
            )
            if not usage:
                counts["records_without_usage"] += 1
                continue
            digest = hashlib.sha256(signature.encode()).hexdigest()
            if digest in seen:
                counts["duplicate_usage_records_excluded"] += 1
                continue
            seen.add(digest)
            counts["records_with_usage"] += 1
            requests = metrics.get("model_requests", [])
            counts["request_metric_entries"] += len(requests)
            counts["format_retry_entries"] += sum("retry" in q["stage"] for q in requests)
            for value in usage.values():
                for name in ("input_tokens", "output_tokens", "total_tokens"):
                    counts[name] += value.get(name, 0)
                counts["cache_read_tokens"] += value.get("input_token_details", {}).get("cache_read", 0)
        groups[group] = dict(counts)

    with zipfile.ZipFile("docs/p1-evaluation.zip") as archive:
        rows = [
            json.loads(archive.read(name))
            for name in archive.namelist()
            if name.endswith(".json") and Path(name).name.startswith("Q") and name.count("/") == 1
        ]
    for method in ("direct", "rag", "agent"):
        add("p1_" + method, [r for r in rows if r.get("method") == method])
    for folder in sorted(Path("eval/runs").glob("p0-*")):
        if folder.is_dir():
            add(folder.name, [json.loads(p.read_text("utf-8")) for p in sorted(folder.glob("Q*.json"))])
    replay = json.loads(Path("docs/p2-replay.json").read_text("utf-8"))
    add(
        "p2_saved_replay",
        [
            {
                "question_id": f"Q19-replay-{i}",
                "method": "audit",
                "metrics": {
                    "token_usage": row["usage"],
                    "model_requests": row["requests"],
                },
            }
            for i, row in enumerate(replay["attempts"])
        ],
    )
    totals = Counter()
    for counts in groups.values():
        totals.update(counts)
    return {
        "scope": "Local saved P0 runs, frozen P1 archive, saved P2 replay; not an account statement or dated charge window.",
        "groups": groups,
        "totals": dict(totals),
        "actual_cost": None,
        "limitations": [
            "Unrecorded probes, interrupted requests and production MySQL tasks are not included.",
            "Request arrays were not saved in some older runs; their tokens are included if available.",
            "Failed/timeout requests without returned usage may have additional provider-side usage.",
            "Cannot attribute a six-yuan recharge without provider billing times, rates and other key consumers.",
            "No current-price estimate is substituted for historical account deductions.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/usage-audit.json")
    args = parser.parse_args()
    report = audit()
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(report["totals"], ensure_ascii=False))
