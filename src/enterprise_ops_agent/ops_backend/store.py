"""SQLite-backed internal ticket, service metrics, deployment, and remediation store."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from enterprise_ops_agent.ops_backend.seeds import (
    get_seed_deployments,
    get_seed_incidents,
    get_seed_services,
)


class OpsStore:
    """Persistent internal operations backend mutated by MCP tools."""

    def __init__(self, db_path: str | Path, *, locale: Literal["en", "zh"] = "en") -> None:
        self.db_path = Path(db_path)
        self.locale: Literal["en", "zh"] = locale
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.seed_if_empty()

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
                    metrics_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    detail_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                );
                """
            )

    def seed_if_empty(self) -> None:
        """Populate realistic initial enterprise incident and service state once."""
        with self._connect() as conn:
            existing = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
            if existing > 0:
                return

            now = datetime.now(UTC).isoformat()
            conn.executemany(
                """
                INSERT INTO incidents
                (ticket_id, title, service, severity, status, reported_at, symptom)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                get_seed_incidents(self.locale),
            )

            for svc, ver, status, metrics in get_seed_services():
                conn.execute(
                    """
                    INSERT INTO services (service_name, current_version, status, metrics_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (svc, ver, status, json.dumps(metrics, ensure_ascii=False), now),
                )

            conn.executemany(
                """
                INSERT INTO deployments (service_name, version, deployed_at, author, summary)
                VALUES (?, ?, ?, ?, ?)
                """,
                get_seed_deployments(self.locale),
            )

    def list_incidents(self, status: str = "open") -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status == "all":
                rows = conn.execute("SELECT * FROM incidents ORDER BY reported_at DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE status = ? ORDER BY reported_at DESC",
                    (status,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_service_metrics(self, service_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM services WHERE service_name = ?",
                (service_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "service_name": row["service_name"],
            "current_version": row["current_version"],
            "status": row["status"],
            "metrics": json.loads(row["metrics_json"]),
            "updated_at": row["updated_at"],
        }

    def list_known_services(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT service_name FROM services ORDER BY service_name").fetchall()
        return [r["service_name"] for r in rows]

    def search_recent_deployments(self, service_name: str) -> list[dict[str, Any]] | None:
        with self._connect() as conn:
            svc = conn.execute(
                "SELECT 1 FROM services WHERE service_name = ?",
                (service_name,),
            ).fetchone()
            if svc is None:
                return None
            rows = conn.execute(
                """
                SELECT version, deployed_at, author, summary
                FROM deployments
                WHERE service_name = ?
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
                VALUES (?, ?, ?, ?, 'open', ?, ?)
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
                "SELECT * FROM services WHERE service_name = ?",
                (service_name,),
            ).fetchone()
            if row is None:
                return {
                    "ok": False,
                    "error": "SERVICE_NOT_FOUND",
                    "known_services": self.list_known_services(),
                }
            metrics = json.loads(row["metrics_json"])
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
                "UPDATE services SET status = 'healthy', metrics_json = ?, updated_at = ? WHERE service_name = ?",
                (json.dumps(metrics, ensure_ascii=False), now, service_name),
            )
            conn.execute(
                """
                INSERT INTO ops_actions (action_id, action_type, service_name, detail_json, reason, executed_at)
                VALUES (?, 'restart_service', ?, ?, ?, ?)
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
                "SELECT * FROM services WHERE service_name = ?",
                (service_name,),
            ).fetchone()
            if svc is None:
                return {
                    "ok": False,
                    "error": "SERVICE_NOT_FOUND",
                    "known_services": self.list_known_services(),
                }
            deploy = conn.execute(
                "SELECT 1 FROM deployments WHERE service_name = ? AND version = ?",
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
                SET current_version = ?, status = 'healthy', metrics_json = ?, updated_at = ?
                WHERE service_name = ?
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
                    UPDATE services SET status = 'healthy', metrics_json = ?, updated_at = ?
                    WHERE service_name = 'redis-prod-01'
                    """,
                    (json.dumps(redis_metrics, ensure_ascii=False), now),
                )
            conn.execute(
                """
                INSERT INTO ops_actions (action_id, action_type, service_name, detail_json, reason, executed_at)
                VALUES (?, 'rollback_deployment', ?, ?, ?, ?)
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
