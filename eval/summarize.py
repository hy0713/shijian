"""Aggregate observed runs; human and model-assisted judgements stay separate."""

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean


def average(values):
    return round(mean(values), 4) if values else None


def summarize(directory):
    rows = [json.loads(p.read_text("utf-8")) for p in sorted(directory.glob("Q[0-9][0-9]_*.json"))]
    anchor_questions = {
        r["question_id"] for r in rows if r.get("automatic", {}).get("evidence_anchor_recall") is not None
    }
    summary = {}
    for method in ("direct", "rag", "agent"):
        records = [r for r in rows if r["method"] == method]
        valid = [r for r in records if r.get("status") != "failed"]
        metrics = [r["result"]["metrics"] for r in valid]
        tokens = [
            sum(u.get("total_tokens", 0) for u in m["token_usage"].values())
            for m in metrics
            if m.get("token_usage")
        ]
        failed_tokens = [
            sum(u.get("total_tokens", 0) for u in (r.get("metrics") or {}).get("token_usage", {}).values())
            for r in records
            if r.get("status") == "failed" and (r.get("metrics") or {}).get("token_usage")
        ]
        recalls = [
            r["automatic"]["evidence_anchor_recall"]["recall"]
            for r in valid
            if r["automatic"]["evidence_anchor_recall"] is not None
        ]
        checks = [c for r in valid for c in r["automatic"].get("citation_program_checks", [])]
        human = {}
        for field in ("evidence_support_0_to_2", "completeness_0_to_2", "premise_recognition_0_to_2"):
            scored = [
                r["human_review"][field]
                for r in valid
                if r["human_review"].get("reviewer") and r["human_review"].get(field) is not None
            ]
            human[field] = {"reviewed": len(scored), "mean": average(scored)}
        summary[method] = {
            "attempts": len(records),
            "recorded": len(valid),
            "failed": len(records) - len(valid),
            "mean_elapsed_seconds": average([r["elapsed_seconds"] for r in valid]),
            "observed_total_tokens": (sum(tokens) + sum(failed_tokens)) if tokens or failed_tokens else None,
            "failed_observed_tokens": sum(failed_tokens) if failed_tokens else None,
            "token_usage_records": len(tokens),
            "mean_tokens": average(tokens),
            "mean_tool_calls": average([m["tool_calls"] for m in metrics]),
            "mean_anchor_recall": average(recalls),
            "anchor_questions": len(recalls),
            "attempted_anchor_questions": sum(r["question_id"] in anchor_questions for r in records),
            "anchor_recall_failures_zero": average(
                [
                    0 if r.get("status") == "failed" else r["automatic"]["evidence_anchor_recall"]["recall"]
                    for r in records
                    if r["question_id"] in anchor_questions
                ]
            ),
            "program_claim_checks": {"valid": sum(c["valid"] for c in checks), "total": len(checks)},
            "outcomes": dict(
                Counter(
                    r["result"].get("research_outcome", {}).get("status", "not_classified") for r in valid
                )
            ),
            "human_only": human,
            "cost": None,
        }
    attention = [
        {
            "question_id": r["question_id"],
            "question": r.get("question"),
            "method": r["method"],
            "error_type": r.get("error_type"),
            "outcome": r.get("result", {}).get("research_outcome"),
            "file": f"{r['question_id']}_{r['method']}.json",
        }
        for r in rows
        if r.get("status") == "failed"
        or r.get("result", {}).get("research_outcome", {}).get("status")
        in {"incomplete", "insufficient_evidence"}
    ]
    return {
        "methods": summary,
        "attention_cases": attention,
        "notes": [
            "Anchor recall measures only known evidence anchors, not full recall or answer accuracy.",
            "Program checks validate citation locations only. Model support reviews remain separate from human scores.",
            "Single run per question, no repeated random trials or confidence intervals; no quality superiority claimed.",
            "Human quality scores remain null until reviewed; model fees are not estimated. Configured Search/Fetch estimates are separate in metrics.web_usage.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(summarize(args.directory), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", "utf-8")
    else:
        print(report)
