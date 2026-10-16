import hashlib
import json
from pathlib import Path


def test_external_articles_are_labelled_and_hashes_valid():
    docs = json.loads(Path("data/demo/web_pages.json").read_text("utf-8"))
    provenance = json.loads(Path("data/demo/web_provenance.json").read_text("utf-8"))
    assert len(docs) == len(provenance) == 2
    for doc, row in zip(docs, provenance):
        assert doc["source_type"] == "通俗文章"
        assert doc["chapter"] is None
        assert row["revision_id"] in doc["source_url"]
        assert hashlib.sha256(doc["raw_text"].encode()).hexdigest() == row["raw_sha256"]
        assert "CC BY-SA" in doc["provenance_note"]
