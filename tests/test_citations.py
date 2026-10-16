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
