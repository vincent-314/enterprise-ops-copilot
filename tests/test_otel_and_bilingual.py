"""Tests for OTel span export, English knowledge base ACL, and dual storage factory."""

from __future__ import annotations

import json
from pathlib import Path

from enterprise_ops_agent.config import Settings
from enterprise_ops_agent.evals import build_golden_eval_cases
from enterprise_ops_agent.observability import AuditTracer, OtelSpanExporter
from enterprise_ops_agent.ops_backend import OpsStore
from enterprise_ops_agent.rag import KnowledgeStore, parse_markdown_document
from enterprise_ops_agent.storage import (
    create_audit_tracer,
    create_checkpoint_store,
    create_knowledge_store,
    create_ops_store,
)


class _DummyEmbedder:
    _VOCAB = ("vpn", "mfa", "redis", "bastion", "10.88.24.19", "order-api", "security")

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        del model
        return [
            [1.0 if kw in text.lower() else 0.05 for kw in self._VOCAB]
            for text in texts
        ]


def test_otel_exporter_writes_otlp_jsonl(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "otel_spans.jsonl"
    exporter = OtelSpanExporter(jsonl_path=jsonl_path)
    tracer = AuditTracer(tmp_path / "traces.db", otel_exporter=exporter)

    tracer.start_trace(
        trace_id="trc_abc123",
        run_id="run_001",
        user_id="alice",
        user_role="employee",
        model="gpt-4.1-mini",
        user_input="Check VPN-401",
    )
    tracer.record_event(
        trace_id="trc_abc123",
        run_id="run_001",
        step_index=1,
        event_type="tool_executed",
        payload={"tool_name": "search_internal_knowledge", "ok": True},
    )
    tracer.finish_trace(
        trace_id="trc_abc123",
        status="finished",
        stop_reason="completed",
        total_tokens=420,
        total_latency_ms=312.5,
        security_events=[],
    )

    assert jsonl_path.exists()
    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 3  # root + run_started + tool_executed
    root_attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
    assert root_attrs["gen_ai.request.model"]["stringValue"] == "gpt-4.1-mini"
    assert root_attrs["gen_ai.usage.total_tokens"]["intValue"] == "420"
    assert root_attrs["enterprise.user_role"]["stringValue"] == "employee"


async def test_english_knowledge_base_rbac_and_bilingual_eval_cases(tmp_path: Path) -> None:
    en_dir = Path("knowledge_base_en")
    assert en_dir.exists()
    docs = [parse_markdown_document(p) for p in sorted(en_dir.glob("*.md"))]
    assert len(docs) == 5

    store = KnowledgeStore(tmp_path / "kb_en.db")
    embedder = _DummyEmbedder()
    await store.index_documents(docs, embedder)

    emp_hits = await store.search("bastion host 10.88.24.19 redis-prod-01", user_role="employee", embedder=embedder)
    assert all("ops_prod_runbook_secret" not in h.doc_id for h in emp_hits)

    admin_hits = await store.search("bastion host 10.88.24.19 redis-prod-01", user_role="ops_admin", embedder=embedder)
    assert any(h.doc_id == "ops_prod_runbook_secret" for h in admin_hits)

    en_cases = build_golden_eval_cases(locale="en")
    zh_cases = build_golden_eval_cases(locale="zh")
    assert len(en_cases) == 10
    assert len(zh_cases) == 10
    assert en_cases[0].case_id == zh_cases[0].case_id


def test_storage_factory_and_locale_ops_seeds(tmp_path: Path) -> None:
    settings = Settings(
        app_locale="en",
        storage_backend="sqlite",
        rag_db_path=str(tmp_path / "r.db"),
        ops_db_path=str(tmp_path / "o.db"),
        checkpoint_db_path=str(tmp_path / "c.db"),
        trace_db_path=str(tmp_path / "t.db"),
    )
    assert settings.resolve_knowledge_dir().name == "knowledge_base_en"
    assert isinstance(create_knowledge_store(settings), KnowledgeStore)
    ops = create_ops_store(settings)
    assert isinstance(ops, OpsStore)
    incidents = ops.list_incidents("open")
    assert len(incidents) == 1
    assert "Order Checkout API" in incidents[0]["title"]
    assert create_checkpoint_store(settings) is not None
    assert create_audit_tracer(settings) is not None
