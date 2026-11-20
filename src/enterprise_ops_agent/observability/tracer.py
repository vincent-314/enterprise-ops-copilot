"""SQLite-backed full-lifecycle audit tracer for enterprise agent compliance."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from enterprise_ops_agent.observability.otel import OtelSpanExporter


class TraceEvent(BaseModel):
    """Single chronological audit event belonging to an execution trace."""

    id: int | None = None
    trace_id: str
    run_id: str
    step_index: int = 0
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TraceSummary(BaseModel):
    """Top-level metadata for a traced agent execution."""

    trace_id: str
    run_id: str
    user_id: str | None = None
    user_role: str | None = None
    model: str | None = None
    status: str = "running"
    stop_reason: str | None = None
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    security_events: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class AuditTracer:
    """Persists ordered agent execution events so any run can be audited by trace_id."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        otel_exporter: OtelSpanExporter | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.otel_exporter = otel_exporter
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
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    user_id TEXT,
                    user_role TEXT,
                    model TEXT,
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    total_latency_ms REAL NOT NULL DEFAULT 0.0,
                    security_events_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL DEFAULT 0,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_trace_events_trace_id
                ON trace_events(trace_id, id);
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
        """Create a new trace record and log the initial run_started event."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO traces
                (trace_id, run_id, user_id, user_role, model, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
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
        """Append an ordered audit event to an existing trace."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trace_events
                (trace_id, run_id, step_index, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
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
        """Finalize trace status, summary metrics, and export OTel spans if enabled."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE traces
                SET status = ?,
                    stop_reason = ?,
                    total_tokens = ?,
                    total_latency_ms = ?,
                    security_events_json = ?,
                    updated_at = ?
                WHERE trace_id = ?
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
        """Fetch trace summary metadata."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
        if row is None:
            return None
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
            security_events=json.loads(row["security_events_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_events(self, trace_id: str) -> list[TraceEvent]:
        """Return chronological audit events for a trace_id."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY id ASC",
                (trace_id,),
            ).fetchall()
        return [
            TraceEvent(
                id=r["id"],
                trace_id=r["trace_id"],
                run_id=r["run_id"],
                step_index=r["step_index"],
                event_type=r["event_type"],
                payload=json.loads(r["payload_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_latest_trace_id(self) -> str | None:
        """Return the most recently updated trace_id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trace_id FROM traces ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return row["trace_id"] if row else None
