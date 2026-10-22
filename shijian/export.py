"""Research Markdown built from saved answers and evidence, without another model request."""

import re
from urllib.parse import urlsplit


def escape(text):
    return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", str(text))


def markdown(task, evidence):
    result = task["result"]
    lines = [
        "# " + escape(task["question"]),
        "",
        f"任务：{task['task_id']}",
        f"状态：{task['status']} · 模式：{task['mode']} · 创建时间：{task['created_at']}",
        "",
    ]
    if not result:
        lines.extend(["尚无最终回答。已保存的证据不代表已形成历史结论。", ""])
        if task["error"]:
            lines.extend([escape(task["error"]), ""])
        return "\n".join(lines)
    outcome = result.get("research_outcome", {})
    lines.extend(
        [
            f"研究覆盖状态：{escape(outcome.get('status', '未提供'))}",
            "",
            "本记录包含模型生成内容。引用定位、模型审核均不能保证解释或史实正确。",
            "",
        ]
    )
    refs = {}
    for title, rows in [
        ("问题前提", [result["premise"]]),
        ("核心结论", result["conclusions"]),
        ("关键事件与解释", result["events"]),
        ("来源分歧", result["disagreements"]),
    ]:
        if not rows:
            continue
        lines.extend(["## " + title, ""])
        for claim in rows:
            citations = []
            for ref in claim["citations"]:
                e = evidence[ref["evidence_id"]]
                quote = e["text"][ref["start"] - e["start"] : ref["end"] - e["start"]]
                if (
                    not e["start"] <= ref["start"] < ref["end"] <= e["end"]
                    or quote != ref["quote"]
                    or ref["url"] != e["url"]
                    or ref["source_id"] != e["source_id"]
                    or ref["chapter"] != e["chapter"]
                ):
                    raise ValueError("保存的引用与证据快照不一致，无法导出")
                key = (ref["evidence_id"], ref["start"], ref["end"])
                if key not in refs:
                    refs[key] = (len(refs) + 1, ref)
                citations.append(f"[^{refs[key][0]}]")
            lines.extend([f"**{escape(claim['kind'])}**：{escape(claim['text'])}" + "".join(citations), ""])
    lines.extend(["## 证据不足与局限", ""])
    lines.extend("- " + escape(text) for text in result["limitations"])
    lines.extend(["", "## 引用原文", ""])
    for number, ref in refs.values():
        e = evidence[ref["evidence_id"]]
        quote = ref["quote"]
        lines.append(
            f"[^{number}]: {escape(e['title'])} {escape(e.get('chapter') or '')}；"
            f"版本：{escape(e.get('edition') or '未标注')}；原文位置 [{ref['start']},{ref['end']})。"
        )
        url = e.get("url")
        if e.get("language"):
            lines.append(
                "    语种线索（规则识别，可能出错）："
                + escape(e["language"])
                + "；中文回答为模型译述，不是原文。"
            )
        if e.get("possible_reprint_of"):
            lines.append("    独立性：与本任务其他正文高度相似，可能转载，不能当作独立佐证。")
        if url and urlsplit(url).scheme in {"http", "https"}:
            lines.append("    来源：" + url.replace("<", "%3C").replace(">", "%3E"))
        lines.extend(["", "    > " + escape(quote).replace("\n", "\n    > "), ""])
        for label, text in [("前文", e.get("before")), ("后文", e.get("after"))]:
            if text:
                lines.extend([f"    {label}：{escape(text).replace(chr(10), chr(10) + '    ')}", ""])
    return "\n".join(lines) + "\n"
