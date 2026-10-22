"""Bounded evidence-first reading and clause review, with programmatic vetoes.

Readings and fact judgements are model assistance, never independent historical evidence.
"""

import json
from collections import Counter

from shijian.schema import QuoteReadings, SupportItem, SupportReview


def dumped(value):
    return json.dumps(value, ensure_ascii=False)


async def review_answer(model, answer, checks, boundary):
    from shijian.citations import claims

    all_claims = claims(answer)
    quotations, keys = [], {}
    for check in checks:
        if not check["valid"]:
            continue
        for ref in check["citations"]:
            key = (ref["evidence_id"], ref["quote"])
            if key not in keys and len(keys) < 24:
                qid = "q" + str(len(keys) + 1)
                keys[key] = qid
                quotations.append({"quotation_id": qid, **ref})
            ref["quotation_id"] = keys.get(key)
        if any(r["quotation_id"] is None for r in check["citations"]):
            check["valid"] = False
            check["errors"].append("达到本次24条引文释读上限，未核查此条")

    readings = {}
    # The first call sees sources only: no candidate claim or previous verdict to anchor its reading.
    for start in range(0, len(quotations), 8):
        await boundary()
        batch = quotations[start : start + 8]
        response = await model.structured(
            QuoteReadings,
            "逐段用中文忠实释读给定原文，每个quotation_id返回一条。英文原文只作中文译述，不改原文。只报告原文写了什么，不补模型记忆。"
            "特别保留人名主客体、否定、数量、比较及时间先后；不能把先一日、晚一年释成同时。"
            "这是给读者的简短原文释义，不是推理过程，不判断任何尚未提供的回答是否正确。",
            dumped({"quotations": batch}),
        )
        counts = Counter(r.quotation_id for r in response.readings)
        for row in response.readings:
            if counts[row.quotation_id] == 1 and any(q["quotation_id"] == row.quotation_id for q in batch):
                readings[row.quotation_id] = row.literal_meaning

    candidates = [
        {
            "claim_index": i,
            "text": c.text,
            "kind": c.kind,
            "quotation_ids": [r["quotation_id"] for r in checks[i]["citations"]],
        }
        for i, c in enumerate(all_claims)
        if checks[i]["valid"]
    ]
    candidates.extend(
        {"claim_index": len(checks) + i, "text": text, "kind": "证据缺口说明", "quotation_ids": []}
        for i, text in enumerate(answer.limitations)
    )
    # Upper bound prevents a verbose draft causing unlimited audit requests; unreviewed claims fail closed.
    candidates = candidates[:24]
    reviews = []
    for start in range(0, len(candidates), 4):
        await boundary()
        batch = candidates[start : start + 4]
        used = {qid for c in batch for qid in c["quotation_ids"] if qid in readings}
        response = await model.structured(
            SupportReview,
            "对照原文与先前独立释读审核每条主张。先把重要事实拆成facts，每个fragment必须是text的连续子句，"
            "逐项核对主体、宾语、数量、日期、时间比较、因果、否定。原文先一日不能支持同日，"
            "但不要套用这个例子来代替实际核查其他关系。每个fact用quotation_ids指向支持它的原文。"
            "任何重要子命题不支持或有矛盾时，整条不能判支持；coverage_complete只在所有重要事实均覆盖时为true。"
            "忠实释义可算文献记载；推断必须标明推断，相关性不证明因果，未提及不证明不存在。"
            "缺口说明只允许本次研究范围及未确定事项，不允许借此添加未经核实的别书、年代或绝对否定。"
            "可靠二手资料对普通日期事实的明确记载可以支持限定于该材料的表述；未读一手原件不自动推翻正文记载。"
            "外文原文的中文译述须保留语义，不能把译文当原文。分歧只能比较所给来源，不能虚构学界争议。"
            "所有verdict字段严格且仅可取支持、部分支持、不支持。无法核实选不支持，不能填无法判断、不适用或无需核查。"
            "无法判断及纯范围说明可以不列facts，其余每条至少一项。输出简短证据支持说明，不输出内部思维过程。",
            dumped(
                {
                    "sources": [
                        {**q, "model_literal_reading": readings[q["quotation_id"]]}
                        for q in quotations
                        if q["quotation_id"] in used
                    ],
                    "claims": batch,
                }
            ),
        )
        counts = Counter(r.claim_index for r in response.checks)
        for row in response.checks:
            candidate = next((c for c in batch if c["claim_index"] == row.claim_index), None)
            if candidate is None or counts[row.claim_index] != 1:
                continue
            errors = []
            if candidate["kind"] not in {"无法判断", "证据缺口说明"}:
                if not row.facts or not row.coverage_complete:
                    errors.append("没有完整的逐项事实核查")
                for fact in row.facts:
                    if fact.fragment not in candidate["text"]:
                        errors.append("核查子句不是候选主张中的连续原文")
                    if fact.verdict != "支持":
                        errors.append("重要子命题未获支持")
                    if not fact.quotation_ids or any(
                        qid not in candidate["quotation_ids"] or qid not in readings
                        for qid in fact.quotation_ids
                    ):
                        errors.append("子命题未关联到本条有效引文及释读")
            if errors:
                row = row.model_copy(update={"verdict": "不支持", "reason": "；".join(dict.fromkeys(errors))})
            reviews.append(row)
    # Invalid references are rejected without asking the model to rescue them.
    reviews.extend(
        SupportItem(claim_index=c["claim_index"], verdict="不支持", reason="引用定位检查未通过")
        for c in checks
        if not c["valid"]
    )
    audit = [{**q, "literal_meaning": readings.get(q["quotation_id"])} for q in quotations]
    return SupportReview(checks=reviews), audit
