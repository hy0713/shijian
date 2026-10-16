"""Real model comparison. No credentials -> fail explicitly; never writes fictional scores."""

import argparse
import asyncio
import json
import time
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from shijian.agent import ResearchAgent
from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import sessions
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


async def run_one(question, method, mode):
    async with aclosing(ResearchModel(settings())) as model:
        return await run_with_model(question, method, mode, model)


async def run_with_model(question, method, mode, model):
    config = settings()
    corpus = Corpus(sessions(), Vectors(config))
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
            message = await model.llm.ainvoke(
                [
                    SystemMessage(
                        content="你是历史问答助手。请直接回答问题，不使用检索工具，明确不确定性，不编造引用。"
                    ),
                    HumanMessage(content=question["question"]),
                ],
                config={"callbacks": [model.callback]},
            )
        result = {
            "answer_text": message.content,
            "metrics": {"tool_calls": 0, "token_usage": model.usage(), "cost": None},
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
                result = (await agent.check(state))["result"]
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
    config = settings()
    if error := config.model_error():
        raise SystemExit(error)
    if args.mode == "hybrid" and not config.search_api_key.get_secret_value():
        raise SystemExit("hybrid 评测缺少 SEARCH_API_KEY")
    questions = json.loads(Path("eval/questions.json").read_text("utf-8"))
    selected = [q for q in questions if not args.ids or q["id"] in args.ids]
    if not selected:
        raise SystemExit("没有匹配的题号")
    target = Path(args.output or "eval/runs/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    target.mkdir(parents=True, exist_ok=True)
    for q in selected:
        for method in args.methods:
            path = target / f"{q['id']}_{method}.json"
            if path.exists():
                print(f"Skip existing {path}")
                continue
            try:
                row = await run_one(q, method, args.mode)
            except Exception as exc:
                # An actual failed attempt is recorded without remote exception text or invented metrics.
                row = {
                    "question_id": q["id"],
                    "method": method,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            path.write_text(json.dumps(row, ensure_ascii=False, indent=2), "utf-8")
            print(path, row.get("status", "recorded_pending_human_review"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--methods", nargs="+", choices=["direct", "rag", "agent"], default=["direct", "rag", "agent"]
    )
    parser.add_argument("--mode", choices=["local", "hybrid"], default="local")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--output")
    asyncio.run(main_async(parser.parse_args()))
