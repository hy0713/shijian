import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from shijian.config import settings
from shijian.corpus import Corpus
from shijian.db import Source, sessions
from shijian.vectors import Vectors


def main():
    parser = argparse.ArgumentParser(description="史鉴资料与索引工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    imp = sub.add_parser("import")
    imp.add_argument("path", type=Path)
    imp.add_argument("--metadata", type=Path, help="TXT/MD 的 DocumentInput 元数据 JSON")
    imp.add_argument("--metadata-only", action="store_true")
    reindex = sub.add_parser("reindex")
    reindex.add_argument("--all", action="store_true", help="将所有资料写入当前集合（更换模型需新集合名）")
    sub.add_parser("embedding-check")
    sub.add_parser("worker")
    search = sub.add_parser("search")
    search.add_argument("query")
    args = parser.parse_args()
    if args.command == "migrate":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        return
    if args.command == "worker":
        from shijian.worker import run_worker

        asyncio.run(run_worker())
        return
    config = settings()
    vectors = Vectors(config)
    corpus = Corpus(sessions(), vectors)
    if args.command == "embedding-check":
        import time

        start = time.monotonic()
        result = vectors.encode(["安祿山", "史思明與安慶緒"])
        print(
            json.dumps(
                {
                    "model": vectors.key,
                    "dimension": len(result[0]),
                    "elapsed_seconds": time.monotonic() - start,
                    "normalized": sum(x * x for x in result[0]),
                    "device": config.embedding_device,
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "import":
        from shijian.api import parse_documents

        metadata = json.loads(args.metadata.read_text("utf-8")) if args.metadata else {}
        for doc in parse_documents(args.path.read_text("utf-8-sig"), args.path.suffix, metadata):
            print(json.dumps(corpus.import_document(doc, index=not args.metadata_only), ensure_ascii=False))
    elif args.command == "reindex":
        with sessions()() as s:
            stmt = select(Source.id)
            if not args.all:
                stmt = stmt.where(Source.index_status != "ready")
            ids = list(s.scalars(stmt))
        for source_id in ids:
            corpus.index_source(source_id)
            print(json.dumps({"source_id": source_id, "index_status": "ready"}))
    elif args.command == "search":
        print(json.dumps(corpus.search(args.query), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
