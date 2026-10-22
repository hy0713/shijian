"""Real model comparison. No credentials -> fail explicitly; never writes fictional scores."""

import argparse
import asyncio
import hashlib
import json
import logging
import time
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from shijian.agent import ResearchAgent
from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import Source, sessions
from shijian.model import ResearchModel
from shijian.vectors import Vectors


def recall(anchors, evidence):
    if not anchors:
        return None
    matched = sum(
        any(
            e["title"] == a["title"] and e["chapter"] == a["chapter"] and a["quote"] in e["text"]
            for e in evidence
        )
        for a in anchors
    )
    return {
        "matched": matched,
        "expected": len(anchors),
        "recall": matched / len(anchors),
        "note": "小规模已知原文锚点的读取召回率；不是全语料完整召回率。",
    }


async def run_one(question, method, mode, shared_corpus=None):
    async with aclosing(ResearchModel(settings())) as model:
        try:
            return await run_with_model(question, method, mode, model, shared_corpus)
        except Exception as exc:
            # Preserve observed usage on failed evaluations without remote exception messages.
            exc.research_metrics = {
                "token_usage": model.usage(),
                "model_requests": model.request_metrics,
                "structured_failures": model.structured_failures,
            }
            raise


async def run_with_model(question, method, mode, model, shared_corpus=None):
    config = settings()
    corpus = shared_corpus or Corpus(sessions(), Vectors(config))
    events, evidence = [], []

    async def emit(kind, data):
        events.append({"type": kind, "data": data})

    async def boundary():
        pass

    async def remember(e):
        evidence.append(e)

    start = time.monotonic()
    if method == "direct":
        async with asyncio.timeout(config.research_timeout):
            message = await model.measured_call(
                "direct",
                [
                    SystemMessage(
                        content="你是历史问答助手。请直接回答问题，不使用检索工具，明确不确定性，不编造引用。"
                    ),
                    HumanMessage(content=question["question"]),
                ],
                model.llm,
            )
        result = {
            "answer_text": message.content,
            "metrics": {
                "tool_calls": 0,
                "token_usage": model.usage(),
                "model_requests": model.request_metrics,
                "cost": None,
            },
        }
    else:
        agent = ResearchAgent(config, model, corpus, emit, boundary, remember)
        if method == "rag":
            hits = await asyncio.to_thread(corpus.search, question["question"], None, 5)
            for hit in hits:
                await agent.add_evidence(await asyncio.to_thread(corpus.read, hit["passage_id"]))
            state = {
                "question": question["question"],
                "analysis": {},
                "limits": [],
                "rounds": 1,
                "tool_calls": 1 + len(hits),
            }
            async with asyncio.timeout(config.research_timeout):
                state.update(await agent.answer(state))
                state.update(await agent.check(state))
                if agent.after_check(state) == "revise":
                    state.update(await agent.revise(state))
                    state.update(await agent.check(state))
                result = state["result"]
        else:
            result = await agent.run(question["question"], mode)
    return {
        "question_id": question["id"],
        "category": question["category"],
        "question": question["question"],
        "method": method,
        "mode": mode if method == "agent" else "local",
        "model": config.deepseek_model,
        "embedding": corpus.vectors.key,
        "result": result,
        "events": events,
        "evidence": evidence,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "automatic": {
            "evidence_anchor_recall": recall(question["expected_evidence"], evidence),
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
    }


async def main_async(args):
    question_path = Path(args.questions)
    questions = json.loads(question_path.read_text("utf-8"))
    selected = [q for q in questions if not args.ids or q["id"] in args.ids]
    if not selected:
        raise SystemExit("没有匹配的题号")
    planned = len(selected) * len(args.methods)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "question_file": str(question_path),
                    "ids": [q["id"] for q in selected],
                    "planned_runs": planned,
                    "mode": args.mode,
                    "paid_requests": 0,
                },
                ensure_ascii=False,
            )
        )
        return
    if planned > args.max_runs:
        raise SystemExit(
            f"计划执行 {planned} 条真实模型评测，超过 --max-runs {args.max_runs}；"
            "请缩小 --ids/--methods，或明确指定更大的 --max-runs。尚未调用模型。"
        )
    config = settings()
    if error := config.model_error():
        raise SystemExit(error)
    if args.mode in {"online", "hybrid"} and (error := config.web_error()):
        raise SystemExit(error)
    target = Path(args.output or "eval/runs/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    target.mkdir(parents=True, exist_ok=True)
    corpus = Corpus(sessions(), Vectors(config))
    with sessions()() as session:
        sources = [
            dict(row._mapping)
            for row in session.execute(
                select(Source.id, Source.title, Source.chapter, Source.content_hash)
                .where(Source.index_status == "ready")
                .order_by(Source.id)
            )
        ]
    if args.source_manifest:
        if args.mode != "local":
            raise SystemExit("固定原文范围的对比仅允许 local 模式，联网补查应单独运行")
        baseline = json.loads(Path(args.source_manifest).read_text("utf-8"))
        current = {s["id"]: s for s in sources}
        if baseline["embedding"] != corpus.vectors.key or any(
            current.get(s["id"]) != s for s in baseline["sources"]
        ):
            raise SystemExit("基线资料缺失、元数据/正文哈希或 Embedding 不一致，停止比较")
        sources = baseline["sources"]
        corpus.source_scope = {s["id"] for s in sources}
    manifest = {
        "model": config.deepseek_model,
        "embedding": corpus.vectors.key,
        "sources": sources,
        "question_ids": [q["id"] for q in selected],
        "methods": args.methods,
        "mode": args.mode,
        "question_file": str(question_path),
        "question_sha256": hashlib.sha256(question_path.read_bytes()).hexdigest(),
        "code_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in [
                "shijian/agent.py",
                "shijian/model.py",
                "shijian/corpus.py",
                "shijian/citations.py",
                "shijian/review.py",
                "shijian/schema.py",
                "shijian/source_reading.py",
            ]
        },
        "budgets": {
            k: getattr(config, k)
            for k in [
                "max_rounds",
                "max_tool_calls",
                "max_evidence",
                "research_timeout",
                "max_model_requests",
                "max_model_tokens",
            ]
        },
    }
    manifest_path = target / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text("utf-8")) != manifest:
        raise SystemExit("模型、语料、预算或代码与该批次不同，请使用新输出目录")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    for q in selected:
        for method in args.methods:
            path = target / f"{q['id']}_{method}.json"
            if path.exists():
                print(f"Skip existing {path}")
                continue
            try:
                row = await run_one(q, method, args.mode, corpus)
            except Exception as exc:
                # An actual failed attempt is recorded without remote exception text or invented metrics.
                row = {
                    "question_id": q["id"],
                    "question": q["question"],
                    "category": q["category"],
                    "model": config.deepseek_model,
                    "method": method,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None),
                    "metrics": getattr(exc, "research_metrics", None),
                    "diagnostic": getattr(exc, "diagnostic", None),
                }
            path.write_text(json.dumps(row, ensure_ascii=False, indent=2), "utf-8")
            print(path, row.get("status", "recorded_pending_human_review"), flush=True)
            if row.get("http_status") in (401, 402):
                print("模型账户不可用，停止后续请求。", flush=True)
                return


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("shijian.model").setLevel(logging.INFO)
    logging.getLogger("shijian.web_usage").setLevel(logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--methods", nargs="+", choices=["direct", "rag", "agent"], default=["direct", "rag", "agent"]
    )
    parser.add_argument("--mode", choices=["local", "hybrid", "online"], default="local")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument(
        "--questions", default="eval/questions.json", help="题集路径；跨主题使用eval/cross_topic.json"
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览执行范围，不连接模型或数据库")
    parser.add_argument("--output")
    parser.add_argument("--max-runs", type=int, default=3, help="本次计划评测条数上限，默认3；不是金额上限")
    parser.add_argument("--source-manifest", help="固定已有批次的资料与哈希，仅用于local可比实验")
    asyncio.run(main_async(parser.parse_args()))
