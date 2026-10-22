"""Scripted protocol fixtures, no historical reasoning or live model capability."""

import asyncio
import json
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from shijian.schema import Analysis, Answer, Assessment, QuoteReadings, SupportReview


class ScriptedModel:
    def __init__(self, config=None, enough_after=2, gate=None, fail=False):
        self.calls = 0
        self.assessments = 0
        self.enough_after = enough_after
        self.gate = gate
        self.fail = fail

    async def choose(self, messages, tools):
        self.calls += 1
        if self.fail:
            raise ConnectionError("TEST-DOUBLE failure, fake-secret-key")
        if self.gate and self.calls == 1:
            await asyncio.to_thread(self.gate.wait, 5)
        if self.calls % 2:
            call = {"name": "search_corpus", "args": {"query": "史思明 安庆绪"}, "id": str(uuid4())}
        else:
            last = next(m for m in reversed(messages) if isinstance(m, ToolMessage))
            hits = json.loads(last.content)
            call = {"name": "read_passage", "args": {"passage_id": hits[0]["passage_id"]}, "id": str(uuid4())}
        return AIMessage(content="", tool_calls=[call])

    async def structured(self, schema, instruction, context):
        if schema is Analysis:
            return Analysis(
                premise_status="待核查",
                premise_to_check=["人物与时序"],
                subquestions=["关系如何"],
                search_queries=["史思明"],
            )
        data = json.loads(context)
        if schema is Assessment:
            self.assessments += 1
            return Assessment(
                sufficient=self.assessments >= self.enough_after,
                premise_status="未确定",
                gaps=[] if self.assessments >= self.enough_after else ["需阅读或补查"],
                next_queries=["安庆绪"],
                summary="TEST-DOUBLE evidence assessment",
            )
        if schema is Answer:
            evidence = data["evidence"]
            return Answer.model_validate(
                {
                    "premise": {"text": "TEST-DOUBLE 未作史实判断", "kind": "无法判断"},
                    "conclusions": [
                        {
                            "text": "TEST-DOUBLE 原文提及史思明",
                            "kind": "文献记载",
                            "citations": [
                                {"evidence_id": evidence[0]["evidence_id"], "quote": evidence[0]["text"][:12]}
                            ],
                        }
                    ]
                    if evidence
                    else [],
                    "events": [],
                    "disagreements": [],
                    "limitations": ["这是协议测试替身，不是真实研究结果"],
                }
            )
        if schema is QuoteReadings:
            return QuoteReadings(
                readings=[
                    {"quotation_id": q["quotation_id"], "literal_meaning": "TEST-DOUBLE literal reading"}
                    for q in data["quotations"]
                ]
            )
        if schema is SupportReview:
            return SupportReview(
                checks=[
                    {
                        "claim_index": row["claim_index"],
                        "verdict": "支持",
                        "reason": "TEST-DOUBLE",
                        "coverage_complete": True,
                        "facts": [
                            {
                                "fragment": row["text"],
                                "quotation_ids": row.get("quotation_ids", []),
                                "verdict": "支持",
                                "reason": "TEST-DOUBLE",
                            }
                        ]
                        if row.get("quotation_ids")
                        else [],
                    }
                    for row in data["claims"]
                ]
            )
        raise AssertionError(schema)

    def usage(self):
        return {"TEST-DOUBLE": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}

    async def aclose(self):
        pass
