"""Download public-domain ancient text paragraphs from fixed Wikisource revisions.

This is an explicit maintainer acquisition command, not the user-facing untrusted fetch tool.
It only requests the fixed Wikimedia host. Snapshots + provenance are committed for offline import.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

PAGES = [
    ("舊唐書/卷200上", "劉昫等奉敕撰", "卷200上"),
    ("新唐書/卷225上", "歐陽修、宋祁等奉敕撰", "卷225上"),
    ("資治通鑑/卷217", "司馬光等編纂", "卷217"),
    ("資治通鑑/卷219", "司馬光等編纂", "卷219"),
    ("資治通鑑/卷220", "司馬光等編纂", "卷220"),
    ("資治通鑑/卷221", "司馬光等編纂", "卷221"),
]


def main():
    target = Path("data/demo")
    target.mkdir(parents=True, exist_ok=True)
    manifest, docs = [], []
    with httpx.Client(
        timeout=45,
        follow_redirects=True,
        headers={"User-Agent": "ShijianDemo/0.1 (historical research demo)"},
    ) as client:
        for title, author, chapter in PAGES:
            # Use rendered HTML; revision ID is embedded in the permalink and wgRevisionId.
            response = client.get("https://zh.wikisource.org/wiki/" + title)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            main = max(soup.select(".mw-parser-output"), key=lambda x: len(x.get_text()), default=None)
            if main is None:
                raise ValueError(f"正文缺失：{title}")
            import re

            match = re.search(r'"wgRevisionId"\s*:\s*(\d+)', response.text)
            if match is None:
                permalink = soup.select_one("#t-permalink a")
                match = re.search(r"oldid=(\d+)", permalink["href"] if permalink else "")
            if match is None:
                raise ValueError(f"修订号缺失：{title}")
            revision = match.group(1)
            paragraphs = []
            for p in main.find_all("p"):
                if p.find_parent(["table", "nav"]):
                    continue
                text = p.get_text("", strip=False).strip()
                if len(text) >= 20:
                    paragraphs.append(text)
            raw = "\n\n".join(paragraphs)
            if "史思明" not in raw or len(raw) < 2000:
                raise ValueError(f"正文校验失败：{title}")
            url = f"https://zh.wikisource.org/w/index.php?title={title}&oldid={revision}"
            fetched = datetime.now(timezone.utc).isoformat()
            doc = {
                "title": title.split("/")[0],
                "author": author,
                "source_type": "历史文献",
                "chapter": chapter,
                "edition": f"维基文库数字文本，修订 {revision}，抓取 {fetched[:10]}；底本未独立校勘",
                "source_url": url,
                "raw_text": raw,
                "people": [
                    p
                    for p in ["安祿山", "安慶緒", "史思明", "史朝義", "郭子儀", "李光弼", "烏承恩"]
                    if p in raw
                ],
                "topics": ["安史之亂"],
                "provenance_note": "后出编纂的历史文献，不能仅凭书名认定为事发时一手记录；维基文库转录可能存在讹误，诸书也可能共享史源。",
            }
            docs.append(doc)
            manifest.append(
                {
                    "page": title,
                    "revision_id": revision,
                    "source_url": url,
                    "fetched_at": fetched,
                    "paragraphs": len(paragraphs),
                    "characters": len(raw),
                    "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "license": "古代原文属于公有领域；数字转录来源保留维基文库署名与链接，站点贡献适用 CC BY-SA 条款；不包含现代译注。",
                    "extraction": "mw-parser-output 中非表格 p 元素，保留原字形、标点，段间加两个换行；非影印校勘本。",
                }
            )
            print(title, revision, len(raw))
    (target / "corpus.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "provenance.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
