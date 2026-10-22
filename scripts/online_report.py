"""Summarize saved live-smoke receipts, including failed tasks. Never makes API requests."""

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path


def report(paths):
    runs, totals, seen = [], Counter(), set()
    known_cost = Decimal(0)
    for path in paths:
        saved = json.loads(path.read_text("utf-8"))
        task = saved["task"]
        if task["task_id"] in seen:
            continue
        seen.add(task["task_id"])
        events = saved.get("events", saved.get("initial_events", []) + saved.get("replayed_events", []))
        models = [e["data"] for e in events if e["type"] == "model_usage"]
        external = [
            {k: v for k, v in e["data"].items() if k != "summary"}
            for e in events
            if e["type"] == "external_usage"
        ]
        counts = Counter(model_requests=len(models), external_requests=len(external))
        for row in models:
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                counts[field] += row[field] or 0
            counts["model_usage_unknown"] += row["total_tokens"] is None
        subtotal = Decimal(0)
        for row in external:
            counts[row["kind"] + "_requests"] += 1
            counts["external_billing_unknown"] += row["billable"] is None
            if row["estimated_cost_cny"] is not None:
                subtotal += Decimal(row["estimated_cost_cny"])
        result = task["result"] or {}
        runs.append(
            {
                "file": path.as_posix(),
                "task_id": task["task_id"],
                "status": task["status"],
                "research_outcome": result.get("research_outcome"),
                "counts": dict(counts),
                "known_tool_estimate_cny": str(subtotal),
                "model_requests": models,
                "external_requests": external,
                "citations": len(result.get("citations", [])),
                "checks": saved["checks"],
                "human_review": None,
            }
        )
        totals.update(counts)
        known_cost += subtotal
    return {
        "scope": "Specified live-smoke files only; failed attempts included, task IDs deduplicated.",
        "runs": runs,
        "totals": dict(totals),
        "known_tool_estimate_cny": str(known_cost),
        "total_tool_estimate_cny": None if totals["external_billing_unknown"] else str(known_cost),
        "model_cost_cny": None,
        "actual_account_cost_cny": None,
        "note": "Known tool subtotal excludes unknown billing and all model fees; not an account statement.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.files)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(
        json.dumps({"totals": result["totals"], "known_tool_estimate_cny": result["known_tool_estimate_cny"]})
    )
