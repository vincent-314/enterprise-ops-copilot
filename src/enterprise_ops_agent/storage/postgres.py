"""PostgreSQL + pgvector persistence backends for cloud-native multi-worker deployment."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from enterprise_ops_agent.observability.tracer import TraceEvent, TraceSummary
from enterprise_ops_agent.ops_backend.seeds import (
    get_seed_deployments,
    get_seed_incidents,
    get_seed_services,
)
from enterprise_ops_agent.rag.chunker import ParsedDocument
from enterprise_ops_agent.rag.store import (
    EmbeddingClient,
    SearchHit,
    rank_hybrid_candidates,
    tokenize_mixed,
)
from enterprise_ops_agent.runtime.checkpoints import ExecutionCheckpoint

if TYPE_CHECKING:
    from enterprise_ops_agent.observability.otel import OtelSpanExporter


def _import_psycopg() -> tuple[Any, Any, Any]:
    try:
        import psycopg
        from pgvector.psycopg import register_vector
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL backend requires optional dependencies: "
            "install with `uv sync --extra postgres` or `pip install 'enterprise-ops-copilot[postgres]'`."
        ) from exc
    return psycopg, dict_row, register_vector


class _PostgresMixin:
    def __init__(self, dsn: str, *, register_pgvector: bool = False) -> None:
        self.dsn = dsn
        self._register_pgvector = register_pgvector
        self._psycopg, self._dict_row, self._register_vector = _import_psycopg()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        conn = self._psycopg.connect(self.dsn, row_factory=self._dict_row)
        try:
            if self._register_pgvector:
                self._register_vector(conn)
            with conn:
                yield conn
        finally:
            conn.close()


class PostgresKnowledgeStore(_PostgresMixin):
    """PostgreSQL + pgvector knowledge store enforcing SQL pre-filter RBAC."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn, register_pgvector=False)
        self._init_schema()
        self._register_pgvector = True

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    doc_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    department TEXT NOT NULL,
                    allowed_roles TEXT[] NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL REFERENCES rag_documents(doc_id) ON DELETE CASCADE,
                    section_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    tokens_json JSONB NOT NULL,
                    embedding vector NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rag_documents_roles
                ON rag_documents USING GIN (allowed_roles);
                """
            )

    async def index_documents(
        self,
        docs: list[ParsedDocument],
        embedder: EmbeddingClient,
        *,
        clear_existing: bool = False,
    ) -> int:
        all_texts = [chunk.content for doc in docs for chunk in doc.chunks]
        embeddings = await embedder.embed(all_texts) if all_texts else []

        now = datetime.now(UTC)
        offset = 0
        total_chunks = 0

        with self._connect() as conn:
            if clear_existing:
                conn.execute("DELETE FROM rag_chunks")
                conn.execute("DELETE FROM rag_documents")

            for doc in docs:
                conn.execute(
                    """
                    INSERT INTO rag_documents
                    (doc_id, title, source_path, department, allowed_roles, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (doc_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        source_path = EXCLUDED.source_path,
                        department = EXCLUDED.department,
                        allowed_roles = EXCLUDED.allowed_roles,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        doc.doc_id,
                        doc.title,
                        doc.source_path,
                        doc.department,
                        doc.allowed_roles,
                        now,
                    ),
                )
                conn.execute("DELETE FROM rag_chunks WHERE doc_id = %s", (doc.doc_id,))

                for chunk in doc.chunks:
                    emb = embeddings[offset]
                    offset += 1
                    tokens = tokenize_mixed(chunk.content)
                    conn.execute(
                        """
                        INSERT INTO rag_chunks
                        (chunk_id, doc_id, section_path, chunk_index, content, tokens_json, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector)
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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM rag_documents
                WHERE '*' = ANY(allowed_roles) OR %s = ANY(allowed_roles)
                """,
                (user_role,),
            ).fetchone()
        return int(row["cnt"]) if row else 0

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
        query_embedding = (await embedder.embed([query]))[0]
        emb_literal = json.dumps(query_embedding)
        query_tokens = tokenize_mixed(query)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.chunk_id,
                    c.doc_id,
                    d.title,
                    c.section_path,
                    c.content,
                    d.allowed_roles,
                    c.tokens_json,
                    1.0 - (c.embedding <=> %s::vector) AS vector_score
                FROM rag_chunks c
                JOIN rag_documents d ON c.doc_id = d.doc_id
                WHERE '*' = ANY(d.allowed_roles) OR %s = ANY(d.allowed_roles)
                """,
                (emb_literal, user_role),
            ).fetchall()

        if not rows:
            return []

        docs_tokens: list[list[str]] = []
        vec_scores: list[float] = []
        normalized_rows: list[dict[str, Any]] = []
        for r in rows:
            tok = r["tokens_json"]
            docs_tokens.append(tok if isinstance(tok, list) else json.loads(tok))
            vec_scores.append(float(r["vector_score"] or 0.0))
            normalized_rows.append(
                {
                    "chunk_id": r["chunk_id"],
                    "doc_id": r["doc_id"],
                    "title": r["title"],
                    "section_path": r["section_path"],
                    "content": r["content"],
                    "allowed_roles": list(r["allowed_roles"]),
                }
            )

        return rank_hybrid_candidates(
            chunk_rows=normalized_rows,
            query_tokens=query_tokens,
            docs_tokens=docs_tokens,
            vec_scores=vec_scores,
            top_k=top_k,
            min_vector_score=min_vector_score,
            rrf_k=rrf_k,
        )


class PostgresCheckpointStore(_PostgresMixin):
    """PostgreSQL-backed HITL checkpoint store for horizontal multi-worker scaling."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_status
                ON checkpoints(status, created_at DESC);
                """
            )

    def save(self, checkpoint: ExecutionCheckpoint) -> None:
        now = datetime.now(UTC).isoformat()
        checkpoint.updated_at = now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints
                (checkpoint_id, run_id, status, payload_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (checkpoint_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    payload_json = EXCLUDED.payload_json,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.status,
                    checkpoint.model_dump_json(),
                    checkpoint.created_at,
                    now,
                ),
            )

    def get(self, checkpoint_id: str) -> ExecutionCheckpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM checkpoints WHERE checkpoint_id = %s",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        payload = row["payload_json"]
        if isinstance(payload, dict):
            return ExecutionCheckpoint.model_validate(payload)
        return ExecutionCheckpoint.model_validate_json(payload)

    def list_pending(self) -> list[ExecutionCheckpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM checkpoints WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
        items: list[ExecutionCheckpoint] = []
        for r in rows:
            payload = r["payload_json"]
            if isinstance(payload, dict):
                items.append(ExecutionCheckpoint.model_validate(payload))
            else:
                items.append(ExecutionCheckpoint.model_validate_json(payload))
        return items

    def update_decision(
        self,
        checkpoint_id: str,
        *,
        status: Literal["approved", "rejected", "completed"],
        reviewer: str,
        comment: str = "",
    ) -> ExecutionCheckpoint:
        ckpt = self.get(checkpoint_id)
        if ckpt is None:
            raise KeyError(f"Checkpoint '{checkpoint_id}' not found.")
        ckpt.status = status
        ckpt.reviewer = reviewer
        ckpt.review_comment = comment
        self.save(ckpt)
        return ckpt


class PostgresAuditTracer(_PostgresMixin):
    """PostgreSQL-backed audit tracer with optional OTel span export."""

    def __init__(
        self,
        dsn: str,
        *,
        otel_exporter: OtelSpanExporter | None = None,
    ) -> None:
        super().__init__(dsn)
        self.otel_exporter = otel_exporter
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_traces (
                    trace_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    user_id TEXT,
                    user_role TEXT,
                    model TEXT,
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    total_latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    security_events_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_trace_events (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id TEXT NOT NULL REFERENCES audit_traces(trace_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL DEFAULT 0,
                    event_type TEXT NOT NULL,
                    payload_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pg_trace_events_trace_id
                ON audit_trace_events(trace_id, id);
                """
            )

    def start_trace(
        self,
        *,
        trace_id: str,
        run_id: str,
        user_id: str | None,
        user_role: str | None,
        model: str | None,
        user_input: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_traces
                (trace_id, run_id, user_id, user_role, model, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'running', %s, %s)
                """,
                (trace_id, run_id, user_id, user_role, model, now, now),
            )
        self.record_event(
            trace_id=trace_id,
            run_id=run_id,
            step_index=0,
            event_type="run_started",
            payload={
                "user_id": user_id,
                "user_role": user_role,
                "model": model,
                "user_input": user_input,
            },
        )

    def record_event(
        self,
        *,
        trace_id: str,
        run_id: str,
        step_index: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_trace_events
                (trace_id, run_id, step_index, event_type, payload_json, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    trace_id,
                    run_id,
                    step_index,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )

    def finish_trace(
        self,
        *,
        trace_id: str,
        status: str,
        stop_reason: str,
        total_tokens: int,
        total_latency_ms: float,
        security_events: list[str],
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audit_traces
                SET status = %s,
                    stop_reason = %s,
                    total_tokens = %s,
                    total_latency_ms = %s,
                    security_events_json = %s::jsonb,
                    updated_at = %s
                WHERE trace_id = %s
                """,
                (
                    status,
                    stop_reason,
                    total_tokens,
                    round(total_latency_ms, 1),
                    json.dumps(security_events, ensure_ascii=False),
                    now,
                    trace_id,
                ),
            )
        if self.otel_exporter is not None and self.otel_exporter.enabled:
            summary = self.get_summary(trace_id)
            if summary is not None:
                self.otel_exporter.export(summary, self.get_events(trace_id))

    def get_summary(self, trace_id: str) -> TraceSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_traces WHERE trace_id = %s",
                (trace_id,),
            ).fetchone()
        if row is None:
            return None
        sec = row["security_events_json"]
        return TraceSummary(
            trace_id=row["trace_id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            user_role=row["user_role"],
            model=row["model"],
            status=row["status"],
            stop_reason=row["stop_reason"],
            total_tokens=row["total_tokens"],
            total_latency_ms=row["total_latency_ms"],
            security_events=sec if isinstance(sec, list) else json.loads(sec),
            created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
        )

    def get_events(self, trace_id: str) -> list[TraceEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_trace_events WHERE trace_id = %s ORDER BY id ASC",
                (trace_id,),
            ).fetchall()
        return [
            TraceEvent(
                id=r["id"],
                trace_id=r["trace_id"],
                run_id=r["run_id"],
                step_index=r["step_index"],
                event_type=r["event_type"],
                payload=r["payload_json"]
                if isinstance(r["payload_json"], dict)
                else json.loads(r["payload_json"]),
                created_at=r["created_at"].isoformat(),
            )
            for r in rows
        ]

    def get_latest_trace_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trace_id FROM audit_traces ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return row["trace_id"] if row else None


class PostgresOpsStore(_PostgresMixin):
    """PostgreSQL-backed internal operations store mutated by MCP tools."""

    def __init__(self, dsn: str, *, locale: Literal["en", "zh"] = "en") -> None:
        super().__init__(dsn)
        self.locale: Literal["en", "zh"] = locale
        self._init_schema()
        self.seed_if_empty()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    ticket_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    service TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reported_at TEXT NOT NULL,
                    symptom TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS services (
                    service_name TEXT PRIMARY KEY,
                    current_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metrics_json JSONB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id SERIAL PRIMARY KEY,
                    service_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    deployed_at TEXT NOT NULL,
                    author TEXT NOT NULL,
                    summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ops_actions (
                    action_id TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    detail_json JSONB NOT NULL,
                    reason TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                );
                """
            )

    def seed_if_empty(self) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM services").fetchone()
            if row and int(row["cnt"]) > 0:
                return

            now = datetime.now(UTC).isoformat()
            for inc in get_seed_incidents(self.locale):
                conn.execute(
                    """
                    INSERT INTO incidents
                    (ticket_id, title, service, severity, status, reported_at, symptom)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticket_id) DO NOTHING
                    """,
                    inc,
                )
            for svc, ver, status, metrics in get_seed_services():
                conn.execute(
                    """
                    INSERT INTO services (service_name, current_version, status, metrics_json, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (service_name) DO NOTHING
                    """,
                    (svc, ver, status, json.dumps(metrics, ensure_ascii=False), now),
                )
            for dep in get_seed_deployments(self.locale):
                conn.execute(
                    """
                    INSERT INTO deployments (service_name, version, deployed_at, author, summary)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    dep,
                )

    def list_incidents(self, status: str = "open") -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status == "all":
                rows = conn.execute("SELECT * FROM incidents ORDER BY reported_at DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE status = %s ORDER BY reported_at DESC",
                    (status,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_service_metrics(self, service_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM services WHERE service_name = %s",
                (service_name,),
            ).fetchone()
        if row is None:
            return None
        metrics = row["metrics_json"]
        return {
            "service_name": row["service_name"],
            "current_version": row["current_version"],
            "status": row["status"],
            "metrics": metrics if isinstance(metrics, dict) else json.loads(metrics),
            "updated_at": row["updated_at"],
        }

    def list_known_services(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT service_name FROM services ORDER BY service_name").fetchall()
        return [r["service_name"] for r in rows]

    def search_recent_deployments(self, service_name: str) -> list[dict[str, Any]] | None:
        with self._connect() as conn:
            svc = conn.execute(
                "SELECT 1 FROM services WHERE service_name = %s",
                (service_name,),
            ).fetchone()
            if svc is None:
                return None
            rows = conn.execute(
                """
                SELECT version, deployed_at, author, summary
                FROM deployments
                WHERE service_name = %s
                ORDER BY deployed_at DESC
                """,
                (service_name,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_ticket(
        self,
        *,
        title: str,
        service_name: str,
        severity: str,
        symptom: str,
    ) -> dict[str, Any]:
        ticket_id = f"INC-2026-{uuid.uuid4().hex[:4].upper()}"
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO incidents (ticket_id, title, service, severity, status, reported_at, symptom)
                VALUES (%s, %s, %s, %s, 'open', %s, %s)
                """,
                (ticket_id, title, service_name, severity, now, symptom),
            )
        return {
            "ticket_id": ticket_id,
            "title": title,
            "service": service_name,
            "severity": severity,
            "status": "open",
            "reported_at": now,
            "symptom": symptom,
        }

    def restart_service(self, *, service_name: str, reason: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM services WHERE service_name = %s",
                (service_name,),
            ).fetchone()
            if row is None:
                return {
                    "ok": False,
                    "error": "SERVICE_NOT_FOUND",
                    "known_services": self.list_known_services(),
                }
            raw_metrics = row["metrics_json"]
            metrics = raw_metrics if isinstance(raw_metrics, dict) else json.loads(raw_metrics)
            if "active_connections" in metrics:
                metrics["active_connections"] = 180
            if "p99_latency_ms" in metrics:
                metrics["p99_latency_ms"] = 140
            if "error_rate_pct" in metrics:
                metrics["error_rate_pct"] = 0.1
            metrics["recent_log_excerpt"] = f"Service restarted successfully: {reason}"

            now = datetime.now(UTC).isoformat()
            action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
            conn.execute(
                "UPDATE services SET status = 'healthy', metrics_json = %s::jsonb, updated_at = %s WHERE service_name = %s",
                (json.dumps(metrics, ensure_ascii=False), now, service_name),
            )
            conn.execute(
                """
                INSERT INTO ops_actions (action_id, action_type, service_name, detail_json, reason, executed_at)
                VALUES (%s, 'restart_service', %s, %s::jsonb, %s, %s)
                """,
                (action_id, service_name, json.dumps({"status": "healthy"}), reason, now),
            )
        return {
            "ok": True,
            "action_id": action_id,
            "service_name": service_name,
            "new_status": "healthy",
            "executed_at": now,
        }

    def rollback_deployment(
        self,
        *,
        service_name: str,
        target_version: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            svc = conn.execute(
                "SELECT * FROM services WHERE service_name = %s",
                (service_name,),
            ).fetchone()
            if svc is None:
                return {
                    "ok": False,
                    "error": "SERVICE_NOT_FOUND",
                    "known_services": self.list_known_services(),
                }
            deploy = conn.execute(
                "SELECT 1 FROM deployments WHERE service_name = %s AND version = %s",
                (service_name, target_version),
            ).fetchone()
            if deploy is None:
                return {
                    "ok": False,
                    "error": "VERSION_NOT_FOUND",
                    "message": f"Version '{target_version}' not found for '{service_name}'.",
                }

            previous_version = svc["current_version"]
            now = datetime.now(UTC).isoformat()
            action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
            metrics = {
                "cpu_pct": 28.0,
                "memory_pct": 52.0,
                "error_rate_pct": 0.1,
                "p99_latency_ms": 125,
                "downstream_dependency": "redis-prod-01",
                "recent_log_excerpt": f"Rolled back from {previous_version} to {target_version}; RedisPool healthy.",
            }
            conn.execute(
                """
                UPDATE services
                SET current_version = %s, status = 'healthy', metrics_json = %s::jsonb, updated_at = %s
                WHERE service_name = %s
                """,
                (target_version, json.dumps(metrics, ensure_ascii=False), now, service_name),
            )
            if service_name == "order-api":
                redis_metrics = {
                    "cpu_pct": 31.0,
                    "memory_pct": 58.0,
                    "active_connections": 210,
                    "max_connections": 10000,
                    "evicted_keys_per_sec": 0,
                    "recent_log_excerpt": "Connection pressure subsided after order-api rollback.",
                }
                conn.execute(
                    """
                    UPDATE services SET status = 'healthy', metrics_json = %s::jsonb, updated_at = %s
                    WHERE service_name = 'redis-prod-01'
                    """,
                    (json.dumps(redis_metrics, ensure_ascii=False), now),
                )
            conn.execute(
                """
                INSERT INTO ops_actions (action_id, action_type, service_name, detail_json, reason, executed_at)
                VALUES (%s, 'rollback_deployment', %s, %s::jsonb, %s, %s)
                """,
                (
                    action_id,
                    service_name,
                    json.dumps({"from": previous_version, "to": target_version}),
                    reason,
                    now,
                ),
            )
        return {
            "ok": True,
            "action_id": action_id,
            "service_name": service_name,
            "previous_version": previous_version,
            "current_version": target_version,
            "new_status": "healthy",
            "executed_at": now,
        }
