from shijian.citations import checked_result, program_check
from shijian.schema import Answer, Citation, Claim, SupportItem, SupportReview


def test_unknown_id_and_fabricated_quote_rejected(corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    e = corpus.read(corpus.search("史思明")[0]["passage_id"])
    unknown = Claim(text="无法确定", kind="无法判断")
    valid = Claim(
        text="文献中提及史思明",
        kind="文献记载",
        citations=[Citation(evidence_id=e["evidence_id"], quote="史思明")],
    )
    bad_id = Claim(
        text="错误引用", kind="文献记载", citations=[Citation(evidence_id="missing", quote="史思明")]
    )
    bad_quote = Claim(
        text="捏造摘录",
        kind="文献记载",
        citations=[Citation(evidence_id=e["evidence_id"], quote="这绝不是原文")],
    )
    missing = Claim(text="没有证据的断言", kind="推断")
    a = Answer(premise=unknown, conclusions=[valid, bad_id, bad_quote, missing], events=[], disagreements=[])
    checks = program_check(a, {e["evidence_id"]: e})
    assert [x["valid"] for x in checks] == [True, True, False, False, False]
    ref = checks[1]["citations"][0]
    assert ref["url"] == e["url"]
    assert demo_docs[0].raw_text[ref["start"] : ref["end"]] == "史思明"
    support = SupportReview(
        checks=[SupportItem(claim_index=i, verdict="支持", reason="TEST-DOUBLE") for i in range(5)]
    )
    result = checked_result(a, {e["evidence_id"]: e}, checks, support, [])
    assert len(result["conclusions"]) == 1 and len(result["rejected_claims"]) == 3
    support.checks[1].verdict = "部分支持"
    assert not checked_result(a, {e["evidence_id"]: e}, checks, support, [])["conclusions"]


def test_limitations_cannot_bypass_content_review():
    answer = Answer(
        premise=Claim(text="当前尚不能判断", kind="无法判断"),
        conclusions=[],
        events=[],
        disagreements=[],
        limitations=["未核对影印本", "某未读文献明确证明所有人物都已决裂"],
    )
    support = SupportReview(
        checks=[
            SupportItem(claim_index=0, verdict="支持", reason="TEST"),
            SupportItem(claim_index=1, verdict="支持", reason="范围声明"),
            SupportItem(claim_index=2, verdict="不支持", reason="未读史料不能作事实来源"),
        ]
    )
    result = checked_result(answer, {}, program_check(answer, {}), support, [])
    assert "未核对影印本" in result["limitations"]
    assert "某未读文献明确证明所有人物都已决裂" not in result["limitations"]
    assert result["rejected_limitations"][0]["text"] == answer.limitations[1]


def test_unknown_core_is_not_a_successful_research_answer():
    answer = Answer(
        premise=Claim(text="尚不能确认", kind="无法判断"),
        conclusions=[Claim(text="缺少证据，无法回答", kind="无法判断")],
        events=[],
        disagreements=[],
    )
    review = SupportReview(
        checks=[SupportItem(claim_index=i, verdict="支持", reason="仅说明未知") for i in range(2)]
    )
    result = checked_result(answer, {}, program_check(answer, {}), review, [])
    assert result["research_outcome"]["status"] == "insufficient_evidence"


def test_budget_limited_supported_core_is_partial(corpus, demo_docs):
    corpus.import_document(demo_docs[0])
    e = corpus.read(corpus.search("史思明")[0]["passage_id"])
    c = Claim(
        text="TEST 文献片段",
        kind="文献记载",
        citations=[Citation(evidence_id=e["evidence_id"], quote=e["text"][:10])],
    )
    answer = Answer(premise=c, conclusions=[c], events=[], disagreements=[])
    review = SupportReview(
        checks=[SupportItem(claim_index=i, verdict="支持", reason="TEST") for i in range(2)]
    )
    result = checked_result(
        answer,
        {e["evidence_id"]: e},
        program_check(answer, {e["evidence_id"]: e}),
        review,
        ["达到工具调用上限"],
    )
    assert result["research_outcome"]["status"] == "partial"
    assert result["conclusions"]
