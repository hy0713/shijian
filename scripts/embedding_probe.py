"""Real BGE-only semantic probe on actual corpus passages, independent of Milvus availability."""

import json
import time
from pathlib import Path

import numpy as np

from shijian.config import settings
from shijian.corpus import split_text
from shijian.vectors import Vectors

docs = json.loads(Path("data/demo/corpus.json").read_text("utf-8"))
# Real quotations from the saved originals, with their exact surrounding chunk.
seeds = [(4, "慶緒忌思明之強"), (1, "初名窣於"), (0, "解六蕃語")]
texts = []
for doc_id, quote in seeds:
    text = docs[doc_id]["raw_text"]
    start, end = next((a, b) for a, b in split_text(text) if quote in text[a:b])
    texts.append(text[start:end])
start = time.monotonic()
vectors = Vectors(settings())
encoded = np.asarray(vectors.encode(texts))
query = np.asarray(vectors.encode(["安庆绪为什么忌惮史思明的强大实力"], query=True)[0])
scores = encoded @ query
order = np.argsort(-scores).tolist()
assert encoded.shape == (3, 512)
assert np.allclose(np.linalg.norm(encoded, axis=1), 1, atol=1e-5)
assert order[0] == 0
print(
    json.dumps(
        {
            "scope": "real_embedding_only_not_milvus_or_agent_evaluation",
            "embedding": vectors.key,
            "device": settings().embedding_device,
            "shape": list(encoded.shape),
            "ranked_seed_indices": order,
            "cosine_scores": scores.tolist(),
            "elapsed_seconds": round(time.monotonic() - start, 3),
        },
        ensure_ascii=False,
    )
)
