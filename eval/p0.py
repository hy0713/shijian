"""Five-case P0 run and integrity/efficiency report; semantic scores require human review."""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from eval.run import main_async

IDS = ["Q01", "Q07", "Q13", "Q19", "Q25"]


def summarize(directory):
    rows = []
    for question_id in IDS:
        path = directory / f"{question_id}_agent.json"
        if not path.exists():
            rows.append({"question_id": question_id, "status": "not_run"})
            continue
        row = json.loads(path.read_text("utf-8"))
        if row.get("status") == "failed":
            rows.append({"question_id": question_id, "status": "failed", "error_type": row["error_type"]})
            continue
        result, evidence = row["result"], {e["evidence_id"]: e for e in row["evidence"]}
        refs = result.get("citations", [])
        valid = []
        for ref in refs:
            e = evidence.get(ref["evidence_id"])
            valid.append(
                bool(
                    e
                    and ref["source_id"] == e["source_id"]
                    and ref["url"] == e["url"]
                    and ref["chapter"] == e["chapter"]
                    and ref["start"] >= e["start"]
                    and ref["end"] <= e["end"]
                    and e["text"][ref["start"] - e["start"] : ref["end"] - e["start"]] == ref["quote"]
                )
            )
        metrics = result["metrics"]
        rows.append(
            {
                "question_id": question_id,
                "question": row["question"],
                "status": "recorded",
                "outcome": result.get("research_outcome"),
                "core_claims": sum(
                    c["kind"] != "无法判断" and bool(c["citations"]) for c in result["conclusions"]
                ),
                "citations": len(refs),
                "citation_integrity": all(valid) if refs else None,
                "anchor_recall": row["automatic"]["evidence_anchor_recall"],
                "tool_calls": metrics["tool_calls"],
                "evidence_read": metrics["evidence_read"],
                "total_tokens": sum(v.get("total_tokens", 0) for v in metrics["token_usage"].values()),
                "elapsed_seconds": metrics["elapsed_seconds"],
                "requests": metrics.get("model_requests"),
                "human_review": row["human_review"],
                "cost": metrics["cost"],
            }
        )
    return rows


def report(after, before=None):
    rows = summarize(after)
    old = summarize(before) if before else []
    paired = [(a, b) for a, b in zip(rows, old) if a["status"] == b["status"] == "recorded"]
    comparison = None
    if paired:
        previous, current = (
            sum(b["total_tokens"] for a, b in paired),
            sum(a["total_tokens"] for a, b in paired),
        )
        comparison = {
            "paired_cases": len(paired),
            "before_tokens": previous,
            "after_tokens": current,
            "token_reduction": 1 - current / previous if previous else None,
            "note": "同一五题、同模型和语料的单次观测；需结合回答质量，不是统计显著性结论。",
        }
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cases": rows,
        "baseline_cases": old,
        "comparison": comparison,
        "note": "引用完整性只检查定位，不证明内容支持。人工语义评分保持未填写，不能用模型审核替代。",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("shijian.model").setLevel(logging.INFO)
    logging.getLogger("shijian.web_usage").setLevel(logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        asyncio.run(
            main_async(
                SimpleNamespace(
                    ids=IDS,
                    methods=["agent"],
                    mode="local",
                    output=str(args.output),
                    max_runs=len(IDS),
                    source_manifest=None,
                )
            )
        )
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "p0-report.json"
    target.write_text(json.dumps(report(args.output, args.baseline), ensure_ascii=False, indent=2), "utf-8")
    print(target)
