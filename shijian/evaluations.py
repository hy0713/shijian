"""Local evaluation artifacts and explicit human review; no model scores presented as human."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from shijian.config import settings

router = APIRouter(prefix="/api/evaluations")


class HumanReview(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    reviewer: str = Field(min_length=1, max_length=80)
    evidence_support_0_to_2: Literal[0, 1, 2]
    completeness_0_to_2: Literal[0, 1, 2]
    premise_recognition_0_to_2: Literal[0, 1, 2]
    notes: str = Field(default="", max_length=4000)


def directory(run_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise HTTPException(400, "评测批次名无效")
    root = Path(settings().evaluation_dir).resolve()
    target = (root / run_id).resolve()
    if target.parent != root or not target.is_dir():
        raise HTTPException(404, "评测批次不存在")
    return target


def result_path(run_id, question_id, method):
    if not re.fullmatch(r"Q\d{2}", question_id) or method not in {"direct", "rag", "agent"}:
        raise HTTPException(400, "题号或方法无效")
    root = directory(run_id)
    target = (root / f"{question_id}_{method}.json").resolve()
    if target.parent != root or not target.is_file():
        raise HTTPException(404, "该题尚未运行")
    return target


def summarize_row(row):
    result = row.get("result", {})
    metrics = result.get("metrics", row.get("metrics") or {})
    return {
        "question_id": row["question_id"],
        "method": row["method"],
        "question": row.get("question"),
        "category": row.get("category"),
        "status": row.get("status", "recorded"),
        "error_type": row.get("error_type"),
        "model": row.get("model"),
        "elapsed_seconds": row.get("elapsed_seconds"),
        "tool_calls": metrics.get("tool_calls"),
        "total_tokens": sum(u.get("total_tokens", 0) for u in metrics.get("token_usage", {}).values())
        if metrics.get("token_usage")
        else None,
        "cost": metrics.get("cost"),
        "outcome": result.get("research_outcome"),
        "anchor_recall": row.get("automatic", {}).get("evidence_anchor_recall"),
        "human_review": row.get("human_review"),
        "checked_citations": len(result.get("citations", [])),
    }


@router.get("")
def runs():
    root = Path(settings().evaluation_dir)
    if not root.exists():
        return []
    return [
        {"run_id": p.name, "records": len(list(p.glob("Q*_*.json")))}
        for p in sorted(root.iterdir(), reverse=True)
        if p.is_dir() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", p.name) and not p.is_symlink()
    ]


@router.get("/{run_id}")
def run(run_id: str):
    root = directory(run_id)
    manifest = root / "manifest.json"
    return {
        "run_id": run_id,
        "manifest": json.loads(manifest.read_text("utf-8")) if manifest.exists() else None,
        "records": [
            summarize_row(
                json.loads(result_path(run_id, p.stem.split("_")[0], p.stem.split("_")[1]).read_text("utf-8"))
            )
            for p in sorted(root.glob("Q[0-9][0-9]_*.json"))
        ],
        "notice": "程序引用检查和模型审核不能替代人工评分；未评分项保持空白，失败记录原样保留。",
    }


@router.get("/{run_id}/{question_id}/{method}")
def case(run_id: str, question_id: str, method: str):
    return json.loads(result_path(run_id, question_id, method).read_text("utf-8"))


@router.put("/{run_id}/{question_id}/{method}/review")
def review(run_id: str, question_id: str, method: str, body: HumanReview):
    path = result_path(run_id, question_id, method)
    row = json.loads(path.read_text("utf-8"))
    if row.get("status") == "failed":
        raise HTTPException(409, "失败任务没有回答，请在失败案例中分析，不填写回答质量分数")
    row["human_review"] = body.model_dump()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as f:
        json.dump(row, f, ensure_ascii=False, indent=2)
        temp = f.name
    os.replace(temp, path)
    return row["human_review"]
