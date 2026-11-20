"""Knowledge ingestion script: parses Markdown docs and indexes them into SQLite or Postgres."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

from enterprise_ops_agent.config import get_settings
from enterprise_ops_agent.llm import LLMClient
from enterprise_ops_agent.rag import KnowledgeStore, parse_markdown_document
from enterprise_ops_agent.storage import create_knowledge_store

console = Console()


async def ingest_directory(docs_dir: Path, db_path: Path | None = None) -> None:
    settings = get_settings()
    md_files = sorted(docs_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"No markdown files found in {docs_dir}")

    parsed_docs = [parse_markdown_document(path) for path in md_files]
    store = (
        KnowledgeStore(db_path)
        if db_path is not None and settings.storage_backend == "sqlite"
        else create_knowledge_store(settings)
    )
    embedder = LLMClient(settings)
    total_chunks = await store.index_documents(parsed_docs, embedder, clear_existing=True)

    target_desc = (
        settings.postgres_dsn.split("@")[-1]
        if settings.storage_backend == "postgres"
        else str(db_path or settings.rag_db_path)
    )
    table = Table(title=f"Ingested Knowledge Base ({settings.storage_backend}) -> {target_desc}")
    table.add_column("doc_id", style="bold")
    table.add_column("Title")
    table.add_column("Department")
    table.add_column("Allowed Roles")
    table.add_column("Chunks", justify="right")

    for doc in parsed_docs:
        table.add_row(
            doc.doc_id,
            doc.title,
            doc.department,
            ", ".join(doc.allowed_roles),
            str(len(doc.chunks)),
        )
    console.print(table)
    console.print(
        f"[green]Indexed {len(parsed_docs)} documents ({total_chunks} chunks) "
        f"using embedding model '{settings.embedding_model}'.[/green]"
    )


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Ingest internal Markdown docs into RAG store.")
    parser.add_argument("--docs-dir", default=str(settings.resolve_knowledge_dir()))
    parser.add_argument("--db-path", default=settings.rag_db_path)
    args = parser.parse_args()
    asyncio.run(ingest_directory(Path(args.docs_dir), Path(args.db_path)))


if __name__ == "__main__":
    main()
