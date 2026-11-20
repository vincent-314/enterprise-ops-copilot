"""SQLite-backed execution checkpoint store for HITL pause and resume."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from enterprise_ops_agent.llm.client import ToolCallRequest
from enterprise_ops_agent.runtime.types import StepRecord
from enterprise_ops_agent.tools.registry import UserContext


class ExecutionCheckpoint(BaseModel):
    """Serializable snapshot of a paused agent execution awaiting human review."""

    checkpoint_id: str
    run_id: str
    trace_id: str | None = None
    status: Literal["pending", "approved", "rejected", "completed"] = "pending"
    model: str | None = None
    user_context: UserContext | None = None
    messages: list[dict[str, Any]]
    pending_tool_calls: list[ToolCallRequest]
    steps_taken_so_far: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_latency_ms: float = 0.0
    steps: list[StepRecord] = Field(default_factory=list)
    security_events: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    review_comment: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class CheckpointStore:
    """Persists paused agent runs so approvals can resume asynchronously or across restarts."""

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, checkpoint: ExecutionCheckpoint) -> None:
        """Insert or update a checkpoint snapshot."""
        now = datetime.now(UTC).isoformat()
        checkpoint.updated_at = now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (checkpoint_id, run_id, status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
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
        """Load a checkpoint by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return ExecutionCheckpoint.model_validate_json(row["payload_json"])

    def list_pending(self) -> list[ExecutionCheckpoint]:
        """List all checkpoints currently waiting for human approval."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM checkpoints WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
        return [ExecutionCheckpoint.model_validate_json(r["payload_json"]) for r in rows]

    def update_decision(
        self,
        checkpoint_id: str,
        *,
        status: Literal["approved", "rejected", "completed"],
        reviewer: str,
        comment: str = "",
    ) -> ExecutionCheckpoint:
        """Record human review decision on a checkpoint."""
        ckpt = self.get(checkpoint_id)
        if ckpt is None:
            raise KeyError(f"Checkpoint '{checkpoint_id}' not found.")
        ckpt.status = status
        ckpt.reviewer = reviewer
        ckpt.review_comment = comment
        self.save(ckpt)
        return ckpt
