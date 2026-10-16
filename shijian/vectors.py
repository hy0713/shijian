import json
from functools import cached_property

from shijian.config import Settings


class Vectors:
    def __init__(self, config: Settings):
        self.config = config
        self.key = f"{config.embedding_model}@{config.embedding_revision}:{config.embedding_dimension}:cls-normalized:v1"

    @cached_property
    def encoder(self):
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            self.config.embedding_model,
            revision=self.config.embedding_revision,
            device=self.config.embedding_device,
            cache_folder=self.config.model_cache,
            trust_remote_code=False,
        )
        if model.get_embedding_dimension() != self.config.embedding_dimension:
            raise ValueError("Embedding 维度与配置不一致，必须更改维度及重建索引")
        model.max_seq_length = 512
        return model

    def encode(self, texts: list[str], query: bool = False):
        if query:
            texts = ["为这个句子生成表示以用于检索相关文章：" + t for t in texts]
        return self.encoder.encode(texts, batch_size=16, normalize_embeddings=True).tolist()

    @cached_property
    def client(self):
        from pymilvus import MilvusClient

        client = MilvusClient(uri=self.config.milvus_uri, timeout=self.config.request_timeout)
        name = self.config.milvus_collection
        if not client.has_collection(name):
            client.create_collection(
                name,
                dimension=self.config.embedding_dimension,
                primary_field_name="id",
                id_type="string",
                max_length=36,
                metric_type="COSINE",
                consistency_level="Strong",
                description=self.key,
            )
        info = client.describe_collection(name)
        if info["description"] != self.key:
            raise ValueError("Milvus 集合的 Embedding 配置不匹配，请使用新集合名并重建索引")
        return client

    def upsert(self, passages):
        for start in range(0, len(passages), 32):
            batch = passages[start : start + 32]
            vectors = self.encode([p.text for p in batch])
            self.client.upsert(
                self.config.milvus_collection,
                [{"id": p.id, "source_id": p.source_id, "vector": v} for p, v in zip(batch, vectors)],
            )

    def search(self, query, source_ids, limit):
        results = self.client.search(
            self.config.milvus_collection,
            data=self.encode([query], query=True),
            filter="source_id in " + json.dumps(source_ids),
            limit=limit,
            output_fields=["source_id"],
        )
        return [hit["id"] for hit in results[0]]
