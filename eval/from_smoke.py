"""Register a saved worker smoke result for the existing human-review UI. No API requests."""

import argparse
import hashlib
import json
from pathlib import Path


def convert(smoke_path, question_id, questions_path):
    saved = json.loads(smoke_path.read_text("utf-8"))
    question = next(q for q in json.loads(questions_path.read_text("utf-8")) if q["id"] == question_id)
    task = saved["task"]
    if task["status"] not in {"completed", "failed", "cancelled"}:
        raise ValueError("只能归档已结束的任务，不能把运行中快照当成结果")
    if task["question"] != question["question"]:
        raise ValueError("任务问题与题集不一致，不能冒充该题评测")
    result = task["result"] or {}
    events = saved.get("events", saved.get("initial_events", []) + saved.get("replayed_events", []))
    diagnostic = next((e["data"] for e in reversed(events) if e["type"] == "diagnostic"), {})
    metrics = result.get("metrics", diagnostic)
    return {
        "question_id": question_id,
        "category": question["category"],
        "question": task["question"],
        "method": "agent",
        "mode": task["mode"],
        "status": "failed" if task["status"] == "failed" else "recorded",
        "error_type": diagnostic.get("error_type"),
        "model": next(iter(metrics.get("token_usage", {})), None),
        "result": result,
        "metrics": metrics,
        "evidence": saved["evidence"],
        "events": events,
        "elapsed_seconds": metrics.get("elapsed_seconds"),
        "automatic": {
            "evidence_anchor_recall": None,
            "citation_program_checks": result.get("citation_audit", {}).get("program", []),
        },
        "model_assisted": {"support_review": result.get("citation_audit", {}).get("content")},
        "human_review": {
            "reviewer": None,
            "evidence_support_0_to_2": None,
            "completeness_0_to_2": None,
            "premise_recognition_0_to_2": None,
            "notes": None,
        },
        "human_rubric": question["human_rubric"],
        "provenance": {
            "task_id": task["task_id"],
            "smoke_sha256": hashlib.sha256(smoke_path.read_bytes()).hexdigest(),
            "smoke_checks": saved["checks"],
            "note": "Registered from a real worker run; registration made no model calls. Human review remains empty.",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("smoke", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--questions", type=Path, default=Path("eval/cross_topic.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    row = convert(args.smoke, args.id, args.questions)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / f"{args.id}_agent.json"
    # Preserve prior failure/success records. A rerun belongs in a new batch directory.
    with target.open("x", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=2)
    print(target)
