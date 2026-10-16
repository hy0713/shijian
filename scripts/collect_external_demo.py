"""Acquire two explicitly labelled CC BY-SA popular articles as web import examples."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

docs, provenance = [], []
with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "ShijianDemo/0.1"}) as client:
    for title in ("安史之乱", "史思明"):
        r = client.get("https://zh.wikipedia.org/wiki/" + title)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        root = max(soup.select(".mw-parser-output"), key=lambda x: len(x.get_text()))
        revision = re.search(r'"wgRevisionId"\s*:\s*(\d+)', r.text)
        if not revision:
            raise ValueError("Wikipedia revision missing")
        paragraphs = []
        for p in root.find_all("p"):
            if p.find_parent("table"):
                continue
            # Exclude footnote markers, retain the text itself and declare the extraction.
            for sup in p.select("sup.reference"):
                sup.decompose()
            text = p.get_text().strip()
            if len(text) >= 40:
                paragraphs.append(text)
            if len(paragraphs) == 6:
                break
        raw = "\n\n".join(paragraphs)
        if len(raw) < 100 or "史思明" not in raw:
            raise ValueError("Wikipedia body missing")
        url = f"https://zh.wikipedia.org/w/index.php?title={title}&oldid={revision[1]}"
        docs.append(
            {
                "title": "维基百科：" + title,
                "author": "维基百科贡献者（见来源页面历史记录）",
                "source_type": "通俗文章",
                "chapter": None,
                "edition": f"页面修订 {revision[1]}，前六个正文段落摘录；2026-10-16 获取",
                "source_url": url,
                "raw_text": raw,
                "people": ["安祿山", "史思明", "安慶緒"],
                "topics": ["安史之亂"],
                "provenance_note": "现代协作百科条目，非学术研究、非独立古代史源；按 CC BY-SA 4.0 署名和相同方式共享保留贡献者历史链接。不能与其引用的古籍重复计作独立证词。",
            }
        )
        provenance.append(
            {
                "title": title,
                "url": url,
                "revision_id": revision[1],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "license": "CC BY-SA 4.0",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "extraction": "前六个非表格正文段落，去除 sup.reference 脚注标号；不含图片。",
            }
        )
        print(title, revision[1], len(raw))
Path("data/demo/web_pages.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2), "utf-8")
Path("data/demo/web_provenance.json").write_text(
    json.dumps(provenance, ensure_ascii=False, indent=2), "utf-8"
)
