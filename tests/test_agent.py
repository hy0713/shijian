import pytest

from shijian.agent import ResearchAgent, answer_evidence
from shijian.config import Settings
from tests.fakes import ScriptedModel


async def make_agent(corpus, model, **kwargs):
    events, evidence = [], []

    async def emit(kind, data):
        events.append((kind, data))

    async def boundary():
        pass

    async def remember(e):
        evidence.append(e)

    agent = ResearchAgent(Settings(_env_file=None, **kwargs), model, corpus, emit, boundary, remember)
    return agent, events, evidence


async def test_real_langgraph_search_read_then_stop(corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    model = ScriptedModel(enough_after=2)
    agent, events, evidence = await make_agent(corpus, model)
    result = await agent.run("为什么安禄山和史思明翻脸", "local")
    assert result["metrics"]["rounds"] == 2
    assert result["metrics"]["tool_calls"] == 2
    assert len(evidence) == 1 + bool(evidence[0]["prev_id"]) + bool(evidence[0]["next_id"])
    assert result["citations"][0]["evidence_id"] == evidence[0]["evidence_id"]
    assert [d["name"] for k, d in events if k == "tool"] == ["search_corpus", "read_passage"]
    assert "assess" in agent.graph.get_graph().nodes


def test_quote_options_are_exact_substrings_of_read_evidence(demo_docs):
    original = demo_docs[0].raw_text[:420]
    evidence = {"evidence_id": "test", "text": original, "before": "不得引用的相邻正文", "after": ""}
    prepared = answer_evidence([evidence])[0]
    assert prepared["quote_options"]
    assert all(option in original for option in prepared["quote_options"])
    assert prepared["text"] == original and "quote_options" not in evidence


async def test_adjacent_originals_are_registered_with_their_own_ids(corpus, demo_docs):
    from sqlalchemy import select

    from shijian.citations import program_check
    from shijian.db import Passage
    from shijian.schema import Answer, Citation, Claim

    imported = corpus.import_document(demo_docs[0])
    with corpus.sessions() as s:
        ids = list(
            s.scalars(
                select(Passage.id).where(Passage.source_id == imported["source_id"]).order_by(Passage.ordinal)
            )
        )
    center = corpus.read(ids[1])
    agent, _, remembered = await make_agent(corpus, ScriptedModel())
    await agent.add_evidence(center)
    neighbor = center["context_passages"][0]
    quote = neighbor["text"][:40]
    assert quote not in center["text"]
    candidate = Claim(
        text="TEST-ONLY",
        kind="文献记载",
        citations=[Citation(evidence_id=center["evidence_id"], quote=quote)],
    )
    answer = Answer(premise=candidate, conclusions=[], events=[], disagreements=[])
    check = program_check(answer, agent.evidence)[0]
    assert check["valid"] and check["citations"][0]["evidence_id"] == neighbor["evidence_id"]
    ref = check["citations"][0]
    assert demo_docs[0].raw_text[ref["start"] : ref["end"]] == quote
    assert neighbor["evidence_id"] in {e["evidence_id"] for e in remembered}
    # Reading some unrelated passage does not permit resolving a wrong ID to arbitrary task evidence.
    unrelated = corpus.read(ids[-1])
    await agent.add_evidence(unrelated)
    candidate.citations[0].quote = unrelated["text"]
    assert not program_check(answer, agent.evidence)[0]["valid"]


async def test_evidence_gap_causes_loop_with_hard_limit(corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    model = ScriptedModel(enough_after=20)
    agent, events, evidence = await make_agent(corpus, model, max_rounds=3, max_tool_calls=3)
    result = await agent.run("测试补查", "local")
    assert model.calls == 3 and result["metrics"]["rounds"] == 3
    assert len([e for e in events if e[0] == "assessment"]) == 3
    assert any("上限" in gap for gap in result["limitations"])


async def test_unread_evidence_not_citable_and_local_web_block(corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    agent, _, _ = await make_agent(corpus, ScriptedModel(), max_rounds=1)
    result = await agent.run("测试", "local")
    assert result["citations"] == [] and result["metrics"]["evidence_read"] == 0
    with pytest.raises(ValueError, match="先"):
        await agent.tools["read_passage"].ainvoke({"passage_id": "model-invented"})
    with pytest.raises(ValueError, match="本地模式"):
        await agent.tools["search_web"].ainvoke({"query": "测试"})


async def test_timeout_preserves_evidence_without_fabricated_answer(corpus, demo_docs):
    corpus.import_document(demo_docs[0])

    class SlowModel(ScriptedModel):
        async def structured(self, schema, instruction, context):
            from shijian.schema import Answer

            if schema is Answer:
                raise TimeoutError()
            return await super().structured(schema, instruction, context)

    agent, _, evidence = await make_agent(corpus, SlowModel())
    result = await agent.run("超时测试", "local")
    assert evidence and result["sources"]
    assert not result["conclusions"] and "超时" in result["limitations"][0]
