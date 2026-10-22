"""P1 source/navigation tests. English bodies below are explicit protocol fixtures, not history."""

import json
from argparse import Namespace
from collections import Counter
from pathlib import Path

import pytest

from shijian.agent import ResearchAgent
from shijian.config import Settings
from shijian.corpus import split_text
from shijian.schema import Answer, DocumentInput
from shijian.source_reading import (
    body_shingles,
    language_hint,
    page_outline,
    possible_reprints,
    publisher_hint,
    rank_leads,
)
from shijian.web import FetchError
from tests.fakes import ScriptedModel


def test_source_hints_are_bounded_and_not_truth_labels():
    assert publisher_hint("https://evil.gov.example.com/") == "来源性质待核查"
    hits = rank_leads(
        [
            {"url": "https://example.com/a", "title": "TEST blog", "snippet": "TEST"},
            {"url": "https://archives.gov/a?utm_source=test", "title": "TEST institution", "snippet": "TEST"},
            {"url": "https://archives.gov/a#fragment", "title": "duplicate", "snippet": "TEST"},
        ]
    )
    assert len(hits) == 2 and hits[0]["url"] == "https://archives.gov/a"
    assert "不保证" in hits[0]["selection_note"]
    assert publisher_hint("https://www.jacar.archives.go.jp/") == "机构或学术域名线索"
    assert publisher_hint("https://jds.cass.cn/") == "机构或学术域名线索"
    assert language_hint("TEST English title. 千九百一年北京ニ於テ調印 シユワルツエンスタイン") == "含日文"


def test_reprint_hint_does_not_merge_or_claim_independence():
    body = " ".join(f"TEST ARTICLE {i} original quotation and source boundary" for i in range(150))
    fp = body_shingles(body)
    assert (
        possible_reprints(body_shingles(body.replace(" ", "\n") + " TEST EDIT"), {"s1": fp})[0]["source_id"]
        == "s1"
    )
    assert not possible_reprints(body_shingles("Completely different text. " * 20), {"s1": fp})
    assert not possible_reprints(body_shingles("short date"), {"s1": fp})


def test_english_sentence_boundaries_and_exact_offsets():
    text = ("TEST ONLY: This sentence is exactly about an archival fixture. " * 30) + "😀 END"
    spans = split_text(text, 130)
    assert all(0 <= start < end <= len(text) and end - start <= 130 for start, end in spans)
    assert all(text[start:end].rstrip().endswith(".") for start, end in spans[:-1])
    assert text[spans[-1][0] : spans[-1][1]].endswith("😀 END")


async def test_long_page_tail_query_heading_and_reprint_metadata(monkeypatch, corpus):
    from shijian import agent as module
    from shijian import deepseek_web

    body = "# TEST FRONT\n\n" + "\n\n".join(
        f"TEST paragraph {i}: no target in this earlier fixture." for i in range(250)
    )
    body += "\n\n# TEST FINAL SECTION\n\nTEST_NEEDLE: This is the relevant tail. 😀 Exact English quotation."
    old = corpus.import_document(
        DocumentInput(title="TEST unrelated local", raw_text="TEST LOCAL ONLY. " * 20), False
    )["source_id"]
    corpus.vectors.fail = True
    events, stored = [], []

    async def emit(kind, data):
        events.append((kind, data))

    async def nop():
        pass

    async def remember(e):
        stored.append(e)

    async def search(*args):
        return [
            {"url": "https://example.org/" + suffix, "title": "TEST page", "snippet": "TEST lead"}
            for suffix in ("first", "reprint")
        ]

    async def fetch(url, *args):
        return {
            "url": url,
            "title": "TEST English source",
            "text": body + ("\nTEST MINOR EDIT" if url.endswith("reprint") else ""),
        }

    monkeypatch.setattr(module, "search_web", search)
    monkeypatch.setattr(deepseek_web, "fetch", fetch)
    agent = ResearchAgent(Settings(_env_file=None), ScriptedModel(), corpus, emit, nop, remember)
    agent.mode, agent.question = "online", "中文问题在英文开头没有关键词命中"
    await agent.tools["search_web"].ainvoke({"query": "TEST English query", "language": "en"})
    first = json.loads(await agent.tools["fetch_page"].ainvoke({"url": "https://example.org/first"}))
    assert len(first["passage_ids"]) == 8  # Do not put hundreds of UUIDs in each model context.
    assert first["outline"][-1]["heading"] == "# TEST FINAL SECTION"
    found = json.loads(
        await agent.tools["read_passage"].ainvoke({"source_id": first["source_id"], "query": "TEST_NEEDLE"})
    )
    tail = next(e for e in stored if "TEST_NEEDLE" in e["text"])
    assert "TEST FINAL SECTION" in tail["section_heading"] and tail["language"] == "英文"
    assert any(e["text"] == "# TEST FINAL SECTION" for e in found)
    assert body[tail["start"] : tail["end"]] == tail["text"]
    assert (
        json.loads(
            await agent.tools["read_passage"].ainvoke(
                {"source_id": first["source_id"], "query": "zzzz_unmatched"}
            )
        )["matches"]
        == []
    )
    with pytest.raises(FetchError, match="本次已取得"):
        await agent.tools["read_passage"].ainvoke({"source_id": old, "query": "TEST"})
    second = json.loads(
        await agent.tools["fetch_page"].ainvoke(
            {"url": "https://example.org/reprint", "focus": "TEST_NEEDLE"}
        )
    )
    assert (
        second["source_id"] != first["source_id"]
        and second["possible_reprint_of"][0]["source_id"] == first["source_id"]
    )
    assert [data["query_language"] for kind, data in events if kind == "search_results"] == ["en"]
    assert len([1 for kind, _ in events if kind == "page_read"]) == 2
    # A translated quote is not the exact original and must fail even if its meaning is similar.
    from shijian.citations import program_check

    answer = Answer(
        premise={"text": "TEST", "kind": "无法判断"},
        conclusions=[
            {
                "text": "TEST translated claim",
                "kind": "文献记载",
                "citations": [{"evidence_id": tail["evidence_id"], "quote": "这是一句中文译文"}],
            }
        ],
        events=[],
        disagreements=[],
    )
    assert not program_check(answer, agent.evidence)[1]["valid"]


def test_source_detail_outline_is_exact_and_free(monkeypatch, corpus):
    from fastapi.testclient import TestClient

    from shijian import api

    body = "# TEST HEADER\n\nTEST ONLY original paragraph.\n\n1901年\n\nTEST entry body."
    source = corpus.import_document(DocumentInput(title="TEST page", raw_text=body), False)
    monkeypatch.setattr(api, "sessions", lambda: corpus.sessions)
    monkeypatch.setattr(api, "corpus", lambda: corpus)
    with TestClient(api.app) as c:
        response = c.get("/api/sources/" + source["source_id"])
    assert response.status_code == 200
    assert [h["heading"] for h in response.json()["outline"]] == ["# TEST HEADER", "1901年"]
    assert page_outline(corpus.page_passages(source["source_id"])) == response.json()["outline"]


async def test_cross_topic_dry_run_does_not_access_credentials_or_services(monkeypatch, capsys):
    from eval import run

    def forbidden():
        raise AssertionError("Dry run must not inspect API config or connect services")

    monkeypatch.setattr(run, "settings", forbidden)
    await run.main_async(
        Namespace(
            questions="eval/cross_topic.json", ids=["Q13"], methods=["agent"], mode="online", dry_run=True
        )
    )
    assert json.loads(capsys.readouterr().out)["paid_requests"] == 0
    cases = json.loads(Path("eval/cross_topic.json").read_text("utf-8"))
    assert len(cases) == len({q["id"] for q in cases}) == 30
    assert set(Counter(q["category"] for q in cases).values()) == {6}
    assert all(q["human_review"] is None and not q["expected_evidence"] for q in cases)
