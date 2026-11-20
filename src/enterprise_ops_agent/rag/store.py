"""SQLite-backed ACL-first Hybrid RAG Store (Dense Vector + CJK BM25 + RRF)."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from enterprise_ops_agent.rag.chunker import ParsedDocument

_TOKEN_RE = re.compile(r"[a-z0-9_.\-]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]+")


class EmbeddingClient(Protocol):
    """Protocol for dense embedding generation."""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]: ...


class SearchHit(BaseModel):
    """Single authorized retrieval hit with hybrid score breakdown."""

    chunk_id: str
    doc_id: str
    title: str
    section_path: str
    content: str
    allowed_roles: list[str]
    vector_score: float
    bm25_score: float
    rrf_score: float


def tokenize_mixed(text: str) -> list[str]:
    """Tokenize mixed Chinese/English technical text into ASCII words + CJK unigrams/bigrams."""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if _CJK_SPAN_RE.fullmatch(raw):
            chars = list(raw)
            tokens.extend(chars)
            tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
        else:
            tokens.append(raw)
    return tokens


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _bm25_scores(
    query_tokens: list[str],
    docs_tokens: list[list[str]],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    if not query_tokens or not docs_tokens:
        return [0.0] * len(docs_tokens)

    n_docs = len(docs_tokens)
    avg_dl = sum(len(d) for d in docs_tokens) / max(1, n_docs)
    df: Counter[str] = Counter()
    tfs: list[Counter[str]] = []
    for tokens in docs_tokens:
        counts = Counter(tokens)
        tfs.append(counts)
        for term in counts:
            df[term] += 1

    unique_q = list(dict.fromkeys(query_tokens))
    scores: list[float] = []
    for idx, doc_tokens in enumerate(docs_tokens):
        dl = len(doc_tokens)
        score = 0.0
        tf_map = tfs[idx]
        for term in unique_q:
            freq = tf_map.get(term, 0)
            if freq == 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log((n_docs - n_q + 0.5) / (n_q + 0.5) + 1.0)
            denom = freq + k1 * (1.0 - b + b * (dl / max(1.0, avg_dl)))
            score += idf * ((freq * (k1 + 1.0)) / denom)
        scores.append(score)
    return scores


def rank_hybrid_candidates(
    *,
    chunk_rows: list[dict[str, Any]],
    query_tokens: list[str],
    docs_tokens: list[list[str]],
    vec_scores: list[float],
    top_k: int = 4,
    min_vector_score: float = 0.20,
    rrf_k: int = 60,
) -> list[SearchHit]:
    """Compute BM25 + Vector RRF fusion over already ACL-filtered rows."""
    if not chunk_rows:
        return []

    bm25_list = _bm25_scores(query_tokens, docs_tokens)
    n = len(chunk_rows)
    vec_ranked = sorted(range(n), key=lambda i: vec_scores[i], reverse=True)
    bm25_ranked = sorted(range(n), key=lambda i: bm25_list[i], reverse=True)

    vec_rank_pos = {idx: rank + 1 for rank, idx in enumerate(vec_ranked)}
    bm25_rank_pos = {idx: rank + 1 for rank, idx in enumerate(bm25_ranked)}

    candidates: list[tuple[int, float]] = []
    for idx in range(n):
        v_score = vec_scores[idx]
        b_score = bm25_list[idx]
        if v_score < min_vector_score and b_score <= 0.0:
            continue
        rrf = (1.0 / (rrf_k + vec_rank_pos[idx])) + (
            1.0 / (rrf_k + bm25_rank_pos[idx]) if b_score > 0.0 else 0.0
        )
        candidates.append((idx, rrf))

    candidates.sort(key=lambda item: item[1], reverse=True)

    hits: list[SearchHit] = []
    for idx, rrf in candidates[:top_k]:
        row = chunk_rows[idx]
        hits.append(
            SearchHit(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                title=row["title"],
                section_path=row["section_path"],
                content=row["content"],
                allowed_roles=row["allowed_roles"],
                vector_score=round(vec_scores[idx], 4),
                bm25_score=round(bm25_list[idx], 4),
                rrf_score=round(rrf, 6),
            )
        )
    return hits


class KnowledgeStore:
    """SQLite knowledge index enforcing pre-filter RBAC before hybrid ranking."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    department TEXT NOT NULL,
                    allowed_roles_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    section_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    tokens_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
                );
                """
            )

    async def index_documents(
        self,
        docs: list[ParsedDocument],
        embedder: EmbeddingClient,
        *,
        clear_existing: bool = False,
    ) -> int:
        """Embed and persist documents + chunks into SQLite."""
        all_texts = [chunk.content for doc in docs for chunk in doc.chunks]
        embeddings = await embedder.embed(all_texts) if all_texts else []

        now = datetime.now(UTC).isoformat()
        offset = 0
        total_chunks = 0

        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            if clear_existing:
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM documents")

            for doc in docs:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO documents
                    (doc_id, title, source_path, department, allowed_roles_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc.doc_id,
                        doc.title,
                        doc.source_path,
                        doc.department,
                        json.dumps(doc.allowed_roles, ensure_ascii=False),
                        now,
                    ),
                )
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc.doc_id,))

                for chunk in doc.chunks:
                    emb = embeddings[offset]
                    offset += 1
                    tokens = tokenize_mixed(chunk.content)
                    conn.execute(
                        """
                        INSERT INTO chunks
                        (chunk_id, doc_id, section_path, chunk_index, content, tokens_json, embedding_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.chunk_id,
                            chunk.doc_id,
                            chunk.section_path,
                            chunk.chunk_index,
                            chunk.content,
                            json.dumps(tokens, ensure_ascii=False),
                            json.dumps(emb),
                        ),
                    )
                    total_chunks += 1
        return total_chunks

    def count_accessible_documents(self, user_role: str) -> int:
        """Return how many documents the given role is authorized to search."""
        with self._connect() as conn:
            rows = conn.execute("SELECT allowed_roles_json FROM documents").fetchall()
        count = 0
        for row in rows:
            roles = json.loads(row["allowed_roles_json"])
            if "*" in roles or user_role in roles:
                count += 1
        return count

    async def search(
        self,
        query: str,
        *,
        user_role: str,
        embedder: EmbeddingClient,
        top_k: int = 4,
        min_vector_score: float = 0.20,
        rrf_k: int = 60,
    ) -> list[SearchHit]:
        """Search only documents authorized for user_role using Vector + BM25 RRF."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.chunk_id,
                    c.doc_id,
                    d.title,
                    c.section_path,
                    c.content,
                    d.allowed_roles_json,
                    c.tokens_json,
                    c.embedding_json
                FROM chunks c
                JOIN documents d ON c.doc_id = d.doc_id
                """
            ).fetchall()

        # Pre-filter ACL: unauthorized chunks never enter scoring or LLM context.
        authorized_rows = []
        authorized_roles_map: list[list[str]] = []
        for row in rows:
            roles = json.loads(row["allowed_roles_json"])
            if "*" in roles or user_role in roles:
                authorized_rows.append(row)
                authorized_roles_map.append(roles)

        if not authorized_rows:
            return []

        query_embedding = (await embedder.embed([query]))[0]
        query_tokens = tokenize_mixed(query)

        docs_tokens = [json.loads(r["tokens_json"]) for r in authorized_rows]
        vec_list = [
            _cosine_similarity(query_embedding, json.loads(r["embedding_json"]))
            for r in authorized_rows
        ]
        normalized_rows = [
            {
                "chunk_id": row["chunk_id"],
                "doc_id": row["doc_id"],
                "title": row["title"],
                "section_path": row["section_path"],
                "content": row["content"],
                "allowed_roles": authorized_roles_map[idx],
            }
            for idx, row in enumerate(authorized_rows)
        ]
        return rank_hybrid_candidates(
            chunk_rows=normalized_rows,
            query_tokens=query_tokens,
            docs_tokens=docs_tokens,
            vec_scores=vec_list,
            top_k=top_k,
            min_vector_score=min_vector_score,
            rrf_k=rrf_k,
        )
