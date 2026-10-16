import argparse
import json
from pathlib import Path
from statistics import mean

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
args = parser.parse_args()
rows = [json.loads(p.read_text("utf-8")) for p in args.directory.glob("Q*_*.json")]
summary = {}
for method in ("direct", "rag", "agent"):
    records = [r for r in rows if r["method"] == method]
    valid = [r for r in records if r.get("status") != "failed"]
    recalls = [
        r["automatic"]["evidence_anchor_recall"]["recall"]
        for r in valid
        if r["automatic"]["evidence_anchor_recall"] is not None
    ]
    human = {}
    for field in ("evidence_support_0_to_2", "completeness_0_to_2", "premise_recognition_0_to_2"):
        scored = [r["human_review"][field] for r in valid if r["human_review"].get(field) is not None]
        human[field] = {"reviewed": len(scored), "mean": mean(scored) if scored else None}
    summary[method] = {
        "attempts": len(records),
        "successful": len(valid),
        "failed": len(records) - len(valid),
        "mean_elapsed_seconds": mean(r["elapsed_seconds"] for r in valid) if valid else None,
        "mean_anchor_recall": mean(recalls) if recalls else None,
        "human_only": human,
        "note": "模型内容支持审核保存在单题结果中，不混入人工评分。未评分显示 null。",
    }
print(json.dumps(summary, ensure_ascii=False, indent=2))
