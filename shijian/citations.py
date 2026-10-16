from shijian.schema import Answer, Claim


def claims(answer: Answer) -> list[Claim]:
    return [answer.premise, *answer.conclusions, *answer.events, *answer.disagreements]


def program_check(answer: Answer, evidence: dict[str, dict]):
    """Fail closed. URLs, chapter and positions come from server evidence, never model fields."""
    checks = []
    for i, claim in enumerate(claims(answer)):
        errors, refs = [], []
        if claim.kind != "无法判断" and not claim.citations:
            errors.append("有事实或解释性结论却未引用已读证据")
        if claim.kind == "无法判断" and claim.citations:
            # Unknown statements may still cite what is known, but quotes must pass the same checks.
            pass
        for cite in claim.citations:
            e = evidence.get(cite.evidence_id)
            if e is None:
                errors.append("evidence_id 不属于已读取证据")
            else:
                if cite.quote not in e["text"]:
                    # The tool reads a bundle. Resolve only exact text in an already registered neighbor.
                    # Unknown IDs, other sources, non-adjacent passages and altered quotes still fail.
                    neighbors = [evidence.get(e.get(key)) for key in ("prev_id", "next_id")]
                    e = next(
                        (
                            n
                            for n in neighbors
                            if n and n["source_id"] == e["source_id"] and cite.quote in n["text"]
                        ),
                        None,
                    )
                if e is None:
                    errors.append("摘录不是已读正文或已登记相邻原文中的连续子串")
                    continue
                offset = e["text"].index(cite.quote)
                refs.append(
                    {
                        **cite.model_dump(),
                        "evidence_id": e["evidence_id"],
                        "source_id": e["source_id"],
                        "title": e["title"],
                        "chapter": e["chapter"],
                        "url": e["url"],
                        "start": e["start"] + offset,
                        "end": e["start"] + offset + len(cite.quote),
                    }
                )
        checks.append({"claim_index": i, "valid": not errors, "errors": errors, "citations": refs})
    return checks


def checked_result(answer, evidence, checks, support, limitations):
    support_by_id = {item.claim_index: item for item in support.checks}
    accepted, rejected = {}, []
    for i, claim in enumerate(claims(answer)):
        check, review = checks[i], support_by_id.get(i)
        valid = check["valid"] and review is not None and review.verdict == "支持"
        if valid:
            accepted[i] = {
                **claim.model_dump(),
                "citations": check["citations"],
                "support_review": review.model_dump(),
            }
        else:
            rejected.append(
                {
                    "claim_index": i,
                    "text": claim.text,
                    "candidate_citations": [c.model_dump() for c in claim.citations],
                    "program_errors": check["errors"],
                    "support_review": review.model_dump() if review else None,
                }
            )
    all_claims = claims(answer)
    pos = 1
    result = {
        "premise": accepted.get(
            0, {"text": "当前证据尚不能可靠确认提问前提。", "kind": "无法判断", "citations": []}
        )
    }
    for name in ("conclusions", "events", "disagreements"):
        size = len(getattr(answer, name))
        result[name] = [accepted[i] for i in range(pos, pos + size) if i in accepted]
        pos += size
    result.update(
        {
            "limitations": [*answer.limitations, *limitations],
            "rejected_claims": rejected,
            "citations": [ref for i in accepted for ref in checks[i]["citations"]],
            "sources": list(
                {
                    e["source_id"]: {
                        k: e[k]
                        for k in (
                            "source_id",
                            "title",
                            "chapter",
                            "edition",
                            "url",
                            "source_type",
                            "provenance_note",
                        )
                    }
                    for e in evidence.values()
                }.values()
            ),
            "citation_audit": {
                "program": checks,
                "content": support.model_dump(),
                "note": "内容支持检查由模型辅助完成，不是真实性保证；仍需人工核查原文与史源。",
            },
        }
    )
    if rejected:
        result["limitations"].append(
            f"有 {len(rejected)}/{len(all_claims)} 条候选表述未通过引用或内容支持检查，已从正式回答移除。"
        )
    if not result["conclusions"]:
        result["limitations"].append("现有证据不足以形成通过核查的核心结论。")
    return result
