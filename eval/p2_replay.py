"""Replay a frozen P1 claim and a birthday-corrected candidate.

This is a known-case automatic regression, not independent human scoring or a blind benchmark.
The corrected candidate can still contain other unsupported subclaims; it is not a valid positive gold case.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from shijian.agent import ResearchAgent
from shijian.config import settings
from shijian.model import ResearchModel
from shijian.schema import Answer


async def main(args):
    path = Path(args.input)
    case = json.loads(path.read_text("utf-8"))
    original = Answer.model_validate(case["result"])
    assert "同日生" in original.premise.text
    if args.premise_only:
        original = original.model_copy(
            update={
                "conclusions": [],
                "events": [],
                "disagreements": [],
                "limitations": [],
            }
        )
    config = settings()
    report = {
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "model": config.deepseek_model,
        "protocol": "Known-case audit regression with the exact same read evidence; no new retrieval.",
        "human_review": None,
        "premise_only": args.premise_only,
        "attempts": [],
    }

    async def nop(*args):
        pass

    for i in range(args.repeats + 1):
        control = i == args.repeats
        answer = original.model_copy(deep=True)
        if control:
            answer.premise.text = answer.premise.text.replace("同日生", "生日相接")
        model = ResearchModel(config)
        agent = ResearchAgent(config, model, None, nop, nop, nop)
        agent.evidence = {e["evidence_id"]: e for e in case["evidence"]}
        started = time.monotonic()
        try:
            state = await agent.check(
                {"question": case["question"], "answer": answer, "limits": [], "rounds": 0, "tool_calls": 0}
            )
            result = state["result"]
            row = {
                "variant": "birthday_corrected_candidate" if control else "frozen_bad_claim",
                "premise_accepted": not any(c["claim_index"] == 0 for c in result["rejected_claims"]),
                "result": result,
            }
        except Exception as exc:
            row = {
                "variant": "birthday_corrected_candidate" if control else "frozen_bad_claim",
                "status": "failed",
                "error_type": type(exc).__name__,
                "http_status": getattr(exc, "status_code", None),
                "diagnostic": getattr(exc, "diagnostic", None),
            }
        finally:
            row.update(
                {
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "usage": model.usage(),
                    "requests": model.request_metrics,
                    "structured_failures": model.structured_failures,
                }
            )
            await model.aclose()
        report["attempts"].append(row)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(
            {k: row.get(k) for k in ["variant", "status", "premise_accepted", "elapsed_seconds"]}, flush=True
        )
        if row.get("http_status") in (401, 402):
            print("模型账户不可用，停止后续请求。", flush=True)
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="docs/p1-cases/Q19_agent.json")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--output", default="eval/runs/p2-replay.json")
    parser.add_argument("--premise-only", action="store_true", help="仅审核已知错误前提，降低回放用量")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(main(parser.parse_args()))
