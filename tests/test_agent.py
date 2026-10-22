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


def test_model_evidence_preserves_original_without_duplicate_context(demo_docs):
    original = demo_docs[0].raw_text[:420]
    evidence = {
        "evidence_id": "test",
        "text": original,
        "before": "重复的相邻正文",
        "after": "",
        "context_passages": [{"text": "嵌套相邻原文"}],
    }
    prepared = answer_evidence([evidence])[0]
    assert prepared == {"evidence_id": "test", "text": original}
    assert "context_passages" in evidence


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


@pytest.mark.parametrize("failure", ["output", "budget"])
async def test_revision_failure_keeps_only_previous_checked_result(corpus, demo_docs, failure):
    from shijian.model import ModelBudgetExceeded, ModelOutputError

    class FailedRevisionModel(ScriptedModel):
        answers = 0

        async def structured(self, schema, instruction, context):
            from shijian.schema import Answer

            if schema is Answer:
                self.answers += 1
                if self.answers == 2:
                    error = ModelOutputError if failure == "output" else ModelBudgetExceeded
                    raise error("TEST private failure body")
            return await super().structured(schema, instruction, context)

    corpus.import_document(demo_docs[0])
    model = FailedRevisionModel()
    agent, _, _ = await make_agent(corpus, model)
    # Force a genuine program-level invalid premise on the first answer instead of altering graph nodes.
    original_structured = model.structured

    async def structured(schema, instruction, context):
        from shijian.schema import Answer, Citation

        answer = await original_structured(schema, instruction, context)
        if schema is Answer:
            answer.premise.kind = "文献记载"
            answer.premise.citations = [Citation(evidence_id="unread", quote="TEST ONLY")]
        return answer

    model.structured = structured
    result = await agent.run("TEST revision failure", "local")
    assert model.answers == 2
    assert result["conclusions"] and result["citations"]
    assert any("保留上一版" in text for text in result["limitations"])
    assert result["metrics"]["answer_revisions"] == 1
    assert "TEST private" not in str(result)


async def test_repeated_tool_is_reused_and_stagnation_stops(corpus, demo_docs, monkeypatch):
    from langchain_core.messages import AIMessage

    corpus.import_document(demo_docs[0])

    class RepeatingModel(ScriptedModel):
        async def choose(self, messages, tools):
            self.calls += 1
            return AIMessage(
                content="",
                tool_calls=[{"name": "search_corpus", "args": {"query": "史思明"}, "id": str(self.calls)}],
            )

    original = corpus.search
    calls = []

    def search(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(corpus, "search", search)
    agent, _, _ = await make_agent(corpus, RepeatingModel(enough_after=20), max_rounds=8)
    result = await agent.run("测试重复检索", "local")
    assert len(calls) == 1
    assert result["metrics"]["rounds"] == 3
    assert result["metrics"]["duplicate_tool_calls_avoided"] == 2
    assert result["research_outcome"]["status"] == "insufficient_evidence"
    assert any("连续两轮" in x for x in result["limitations"])


async def test_planning_history_is_bounded_and_keeps_next_queries(corpus, demo_docs):
    import json

    from langchain_core.messages import HumanMessage

    corpus.import_document(demo_docs[0])

    class RecordingModel(ScriptedModel):
        def __init__(self):
            super().__init__(enough_after=20)
            self.contexts = []
            self.allowed_tools = []

        async def choose(self, messages, tools):
            self.contexts.append(messages)
            self.allowed_tools.append([t.name for t in tools])
            return await super().choose(messages, tools)

    model = RecordingModel()
    agent, _, _ = await make_agent(corpus, model, max_rounds=4)
    await agent.run("上下文测试", "local")
    assert max(len(m) for m in model.contexts) <= 4
    assert model.allowed_tools[-1] == ["read_passage"]
    last = json.loads(next(m.content for m in model.contexts[-1] if isinstance(m, HumanMessage)))
    assert last["assessment"]["next_queries"] == ["安庆绪"]
    assert last["completed_tools"] and last["read_evidence"]
    assert all(len(e["preview"]) <= 180 for e in last["read_evidence"])


async def test_invalid_core_revised_once_then_still_rejected(corpus, demo_docs):
    from shijian.schema import Answer

    corpus.import_document(demo_docs[0])

    class BrokenAnswer(ScriptedModel):
        def __init__(self):
            super().__init__()
            self.answers = 0

        async def structured(self, schema, instruction, context):
            result = await super().structured(schema, instruction, context)
            if schema is Answer:
                self.answers += 1
                result.conclusions[0].citations[0].quote = "完全捏造的古文摘录"
            return result

    model = BrokenAnswer()
    agent, events, _ = await make_agent(corpus, model)
    result = await agent.run("修订测试", "local")
    assert model.answers == 2 and result["metrics"]["answer_revisions"] == 1
    assert not result["conclusions"]
    assert result["research_outcome"]["status"] == "insufficient_evidence"
    assert len([e for e in events if e[0] == "step" and "修订" in e[1]["step"]]) == 1


async def test_repair_can_restore_core_without_relaxing_checks(corpus, demo_docs):
    from shijian.schema import Answer

    corpus.import_document(demo_docs[0])

    class RepairingModel(ScriptedModel):
        def __init__(self):
            super().__init__()
            self.answers = 0

        async def structured(self, schema, instruction, context):
            result = await super().structured(schema, instruction, context)
            if schema is Answer:
                self.answers += 1
                if self.answers == 1:
                    result.conclusions[0].citations[0].evidence_id = "unread-fabricated"
            return result

    agent, _, _ = await make_agent(corpus, RepairingModel())
    result = await agent.run("修复测试", "local")
    assert result["conclusions"] and result["metrics"]["answer_revisions"] == 1
    assert all(c["valid"] for c in result["citation_audit"]["program"])


async def test_timeout_outcome_is_incomplete(corpus, demo_docs):
    from shijian.schema import Analysis

    class TimedOut(ScriptedModel):
        async def structured(self, schema, instruction, context):
            if schema is Analysis:
                raise TimeoutError()
            return await super().structured(schema, instruction, context)

    agent, _, _ = await make_agent(corpus, TimedOut())
    result = await agent.run("测试超时", "local")
    assert result["research_outcome"]["status"] == "incomplete"


@pytest.mark.parametrize("during_revision", [False, True])
async def test_budget_during_check_retains_checked_result_only(corpus, demo_docs, during_revision):
    from shijian.model import ModelBudgetExceeded
    from shijian.schema import Answer, Citation, QuoteReadings

    corpus.import_document(demo_docs[0])

    class BudgetModel(ScriptedModel):
        answers = 0

        async def structured(self, schema, instruction, context):
            if schema is QuoteReadings and self.answers == (2 if during_revision else 1):
                raise ModelBudgetExceeded("TEST ONLY")
            result = await super().structured(schema, instruction, context)
            if schema is Answer:
                self.answers += 1
                # Invalid premise triggers revision; core conclusion remains valid in first check.
                result.premise.kind = "文献记载"
                result.premise.citations = [Citation(evidence_id="unread", quote="TEST ONLY")]
            return result

    agent, _, _ = await make_agent(corpus, BudgetModel())
    result = await agent.run("TEST budget", "local")
    if during_revision:
        assert result["conclusions"] and result["citations"]
        assert any("保留上一版" in text for text in result["limitations"])
    else:
        assert not result["conclusions"] and not result["citations"]
        assert result["research_outcome"]["status"] == "incomplete"


async def test_explicit_work_name_scopes_search_in_both_scripts(corpus, demo_docs):
    import json

    for doc in demo_docs:
        corpus.import_document(doc)
    agent, _, _ = await make_agent(corpus, ScriptedModel())
    for query in ("新唐书 史思明 安禄山", "新唐書 史思明 安祿山"):
        hits = json.loads(await agent.tools["search_corpus"].ainvoke({"query": query}))
        assert hits and all(h["title"] == "新唐書" for h in hits)
