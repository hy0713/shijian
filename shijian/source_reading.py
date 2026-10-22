"""Source-selection hints and original-position navigation, without model or network calls."""

import re
from urllib.parse import urlsplit

from shijian.web import canonical_url


def language_hint(text):
    if len(re.findall(r"[\u3040-\u30ff]", text)) >= 3:
        return "含日文"
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if chinese > 10 and latin > chinese:
        return "中英混合"
    return "中文" if chinese > 10 else "英文" if latin > 20 else "未确定"


def publisher_hint(url):
    host = urlsplit(url).hostname or ""
    institutional = host.endswith(
        (".gov", ".gov.cn", ".edu", ".edu.cn", ".ac.cn", ".ac.uk", ".go.jp")
    ) or any(
        host == domain or host.endswith("." + domain)
        for domain in (
            "cambridge.org",
            "bl.uk",
            "britishmuseum.org",
            "un.org",
            "loc.gov",
            "cass.cn",
            "nlc.cn",
        )
    )
    return "机构或学术域名线索" if institutional else "来源性质待核查"


def rank_leads(hits):
    unique = {}
    for hit in hits:
        url = canonical_url(hit["url"])
        unique.setdefault(
            url,
            {
                **hit,
                "url": url,
                "publisher_hint": publisher_hint(url),
                "language_hint": language_hint(hit["title"] + " " + hit["snippet"]),
                "selection_note": "域名与搜索评分仅为选源线索，不保证史实真实或属于一手文献。",
            },
        )
    # Stable ordering within each group preserves the provider's own ranking.
    return sorted(unique.values(), key=lambda h: h["publisher_hint"] != "机构或学术域名线索")


def page_outline(passages):
    headings = []
    for passage in passages:
        text = passage["text"].strip()
        clean = text.replace("\\#", "#")
        if len(clean) <= 160 and (
            re.match(r"^#{1,6}\s+\S", clean)
            or re.fullmatch(r"\d{3,4}年", clean)
            or re.match(r"^第[一二三四五六七八九十百千\d]+[卷章节]\s*", clean)
        ):
            headings.append(
                {
                    "heading": text,
                    "start": passage["start"],
                    "passage_id": passage["id"],
                }
            )
    return headings


def section_at(outline, start):
    return next((h for h in reversed(outline) if h["start"] <= start), None)


def body_shingles(text):
    # Bounded heuristic over the first 60k characters, never a basis for merging/deleting originals.
    normalized = re.sub(r"[\W_]+", "", text[:60_000].casefold())
    return {normalized[i : i + 8] for i in range(len(normalized) - 7)} if len(normalized) >= 120 else set()


def possible_reprints(fingerprint, previous):
    matches = []
    for source_id, other in previous.items():
        if not fingerprint or not other:
            continue
        similarity = len(fingerprint & other) / len(fingerprint | other)
        if similarity >= 0.85:
            matches.append({"source_id": source_id, "similarity": round(similarity, 3)})
    return matches
