import json

import pytest

from shijian.citations import checked_result, program_check
from shijian.review import review_answer
from shijian.schema import Answer, QuoteReadings, SupportReview


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "contradiction",
        "unknown_quote",
        "invented_fragment",
        "incomplete",
        "missing_fact",
        "missing_reading",
        "duplicate_review",
    ],
)
async def test_clause_audit_cannot_be_overridden_by_aggregate_support(fault):
    # Explicit English protocol fixture, not fabricated ancient text or a historical result.
    text = "TEST SOURCE: A happened one day before B."
    evidence = {
        "p": {
            "evidence_id": "p",
            "source_id": "s",
            "title": "TEST ONLY",
            "chapter": None,
            "url": None,
            "text": text,
            "start": 0,
            "end": len(text),
            "edition": "TEST ONLY",
            "source_type": "其他",
            "provenance_note": "TEST DOUBLE",
        }
    }
    answer = Answer.model_validate(
        {
            "premise": {"text": "TEST unknown premise", "kind": "无法判断"},
            "conclusions": [
                {
                    "text": "TEST A happened before B",
                    "kind": "文献记载",
                    "citations": [{"evidence_id": "p", "quote": text}],
                }
            ],
            "events": [],
            "disagreements": [],
        }
    )

    class Reviewer:
        async def structured(self, schema, instruction, context):
            data = json.loads(context)
            if schema is QuoteReadings:
                assert "claims" not in data and "question" not in data
                return QuoteReadings(
                    readings=[]
                    if fault == "missing_reading"
                    else [{"quotation_id": "q1", "literal_meaning": "TEST A was earlier, not simultaneous"}]
                )
            rows = []
            for c in data["claims"]:
                facts = [
                    {
                        "fragment": "invented new fact" if fault == "invented_fragment" else c["text"],
                        "quotation_ids": ["q99" if fault == "unknown_quote" else "q1"],
                        "verdict": "不支持" if fault == "contradiction" else "支持",
                        "reason": "TEST ONLY",
                    }
                ]
                rows.append(
                    {
                        "claim_index": c["claim_index"],
                        "verdict": "支持",
                        "reason": "TEST aggregate",
                        "coverage_complete": fault != "incomplete",
                        "facts": [] if fault == "missing_fact" or not c["quotation_ids"] else facts,
                    }
                )
            if fault == "duplicate_review":
                rows.append(rows[-1])
            return SupportReview(checks=rows)

    async def boundary():
        pass

    checks = program_check(answer, evidence)
    review, audit = await review_answer(Reviewer(), answer, checks, boundary)
    result = checked_result(answer, evidence, checks, review, [])
    assert bool(result["conclusions"]) == (fault is None)
    assert audit[0]["quote"] == text and audit[0]["evidence_id"] == "p"


def test_fixed_source_scope_does_not_leak_newer_documents(corpus, demo_docs):
    from shijian.corpus import Corpus

    first = corpus.import_document(demo_docs[0])["source_id"]
    second = corpus.import_document(demo_docs[1])["source_id"]
    scoped = Corpus(corpus.sessions, corpus.vectors, [first])
    assert all(r["source_id"] == first for r in scoped.search("史思明"))
    assert second not in scoped.mentioned_sources("新唐书")
    assert not Corpus(corpus.sessions, corpus.vectors, []).search("史思明")


def test_markdown_export_uses_saved_evidence_and_rejects_corrupt_references(monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from shijian import api
    from shijian.export import markdown

    row = json.loads(Path("docs/p1-cases/Q19_agent.json").read_text("utf-8"))
    task = {
        "task_id": "test-export",
        "question": row["question"],
        "mode": "local",
        "status": "completed",
        "created_at": "2026-10-20T00:00:00Z",
        "error": None,
        "result": row["result"],
    }
    task["result"]["metrics"]["private_test_field"] = "TEST_PROVIDER_SECRET"
    evidence = {e["evidence_id"]: e for e in row["evidence"]}
    text = markdown(task, evidence)
    assert (
        "同日生" in text and "先祿山一日生" in text
    )  # Preserve the actual failure, do not silently rewrite it.
    assert "[^1]" in text and "oldid=" in text and "原文位置" in text
    assert "TEST_PROVIDER_SECRET" not in text

    class Store:
        def get(self, task_id):
            return task

        def evidence(self, task_id):
            return list(evidence.values())

    monkeypatch.setattr(api, "store", Store)
    with TestClient(api.app) as client:
        response = client.get("/api/tasks/test-export/export")
        assert response.status_code == 200 and "text/markdown" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        task["result"]["premise"]["citations"][0]["url"] = "https://wrong.example/"
        assert client.get("/api/tasks/test-export/export").status_code == 409
