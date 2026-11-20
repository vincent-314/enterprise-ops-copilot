"""Dual-backend storage factory supporting zero-config SQLite and PostgreSQL/pgvector."""

from __future__ import annotations

from typing import Any

from enterprise_ops_agent.config import Settings
from enterprise_ops_agent.observability.otel import OtelSpanExporter
from enterprise_ops_agent.observability.tracer import AuditTracer
from enterprise_ops_agent.ops_backend.store import OpsStore
from enterprise_ops_agent.rag.store import KnowledgeStore
from enterprise_ops_agent.runtime.checkpoints import CheckpointStore


def create_otel_exporter(settings: Settings) -> OtelSpanExporter:
    """Build OTel JSONL / OTLP exporter from settings."""
    return OtelSpanExporter(
        jsonl_path=settings.otel_export_jsonl_path,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )


def create_knowledge_store(settings: Settings) -> Any:
    """Instantiate SQLite or PostgreSQL+pgvector knowledge store."""
    if settings.storage_backend == "postgres":
        from enterprise_ops_agent.storage.postgres import PostgresKnowledgeStore

        return PostgresKnowledgeStore(settings.postgres_dsn)
    return KnowledgeStore(settings.rag_db_path)


def create_ops_store(settings: Settings) -> Any:
    """Instantiate SQLite or PostgreSQL ops backend store."""
    if settings.storage_backend == "postgres":
        from enterprise_ops_agent.storage.postgres import PostgresOpsStore

        return PostgresOpsStore(settings.postgres_dsn, locale=settings.app_locale)
    return OpsStore(settings.ops_db_path, locale=settings.app_locale)


def create_checkpoint_store(settings: Settings) -> Any:
    """Instantiate SQLite or PostgreSQL HITL checkpoint store."""
    if settings.storage_backend == "postgres":
        from enterprise_ops_agent.storage.postgres import PostgresCheckpointStore

        return PostgresCheckpointStore(settings.postgres_dsn)
    return CheckpointStore(settings.checkpoint_db_path)


def create_audit_tracer(settings: Settings) -> Any:
    """Instantiate SQLite or PostgreSQL audit tracer with OTel export hook."""
    exporter = create_otel_exporter(settings)
    if settings.storage_backend == "postgres":
        from enterprise_ops_agent.storage.postgres import PostgresAuditTracer

        return PostgresAuditTracer(settings.postgres_dsn, otel_exporter=exporter)
    return AuditTracer(settings.trace_db_path, otel_exporter=exporter)


__all__ = [
    "create_audit_tracer",
    "create_checkpoint_store",
    "create_knowledge_store",
    "create_ops_store",
    "create_otel_exporter",
]
