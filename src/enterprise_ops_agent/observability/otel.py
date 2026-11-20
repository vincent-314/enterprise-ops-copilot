"""OpenTelemetry / Langfuse-compatible OTLP span builder and exporter."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from enterprise_ops_agent.observability.tracer import TraceEvent, TraceSummary

logger = logging.getLogger(__name__)


def _hex_id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def _iso_to_unix_nano(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return str(int(dt.timestamp() * 1_000_000_000))
    except ValueError:
        return str(int(datetime.now(UTC).timestamp() * 1_000_000_000))


def _otel_attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, list):
        return {
            "key": key,
            "value": {"arrayValue": {"values": [{"stringValue": str(v)} for v in value]}},
        }
    return {"key": key, "value": {"stringValue": str(value)}}


def build_otel_spans(
    summary: TraceSummary,
    events: list[TraceEvent],
    *,
    service_name: str = "enterprise-ops-copilot",
) -> dict[str, Any]:
    """Convert an AuditTracer run into an OTLP/HTTP JSON ExportTraceServiceRequest payload."""
    otel_trace_id = _hex_id(summary.trace_id, 32)
    root_span_id = _hex_id(f"{summary.trace_id}:root", 16)
    start_ns = _iso_to_unix_nano(summary.created_at)
    end_ns = _iso_to_unix_nano(summary.updated_at)

    status_code = 2 if summary.stop_reason in {"error", "blocked_by_guardrail"} else 1
    root_attrs = [
        _otel_attr("gen_ai.system", "openai_compatible"),
        _otel_attr("gen_ai.request.model", summary.model or "unknown"),
        _otel_attr("gen_ai.usage.total_tokens", summary.total_tokens),
        _otel_attr("enterprise.trace_id", summary.trace_id),
        _otel_attr("enterprise.run_id", summary.run_id),
        _otel_attr("enterprise.user_id", summary.user_id or "anonymous"),
        _otel_attr("enterprise.user_role", summary.user_role or "anonymous"),
        _otel_attr("enterprise.status", summary.status),
        _otel_attr("enterprise.stop_reason", summary.stop_reason or "unknown"),
        _otel_attr("enterprise.latency_ms", summary.total_latency_ms),
        _otel_attr("enterprise.security_events", summary.security_events),
    ]

    spans: list[dict[str, Any]] = [
        {
            "traceId": otel_trace_id,
            "spanId": root_span_id,
            "name": "enterprise_ops_agent.run",
            "kind": 1,
            "startTimeUnixNano": start_ns,
            "endTimeUnixNano": end_ns,
            "attributes": root_attrs,
            "status": {"code": status_code},
        }
    ]

    for idx, ev in enumerate(events):
        ev_ns = _iso_to_unix_nano(ev.created_at)
        span_id = _hex_id(f"{summary.trace_id}:ev:{idx}:{ev.event_type}", 16)
        attrs = [
            _otel_attr("enterprise.event_type", ev.event_type),
            _otel_attr("enterprise.step_index", ev.step_index),
            _otel_attr("enterprise.payload_json", json.dumps(ev.payload, ensure_ascii=False)),
        ]
        if "tool_name" in ev.payload:
            attrs.append(_otel_attr("gen_ai.tool.name", ev.payload["tool_name"]))
        spans.append(
            {
                "traceId": otel_trace_id,
                "spanId": span_id,
                "parentSpanId": root_span_id,
                "name": f"agent.{ev.event_type}",
                "kind": 3 if ev.event_type in {"llm_response", "tool_executed"} else 1,
                "startTimeUnixNano": ev_ns,
                "endTimeUnixNano": ev_ns,
                "attributes": attrs,
                "status": {"code": 1},
            }
        )

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_otel_attr("service.name", service_name)]},
                "scopeSpans": [
                    {
                        "scope": {"name": "enterprise_ops_agent.observability", "version": "0.2.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


class OtelSpanExporter:
    """Exports completed/paused agent traces to OTel JSONL files and/or an OTLP endpoint."""

    def __init__(
        self,
        *,
        jsonl_path: str | Path | None = None,
        otlp_endpoint: str | None = None,
        service_name: str = "enterprise-ops-copilot",
    ) -> None:
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.otlp_endpoint = otlp_endpoint.strip() if otlp_endpoint else None
        self.service_name = service_name

    @property
    def enabled(self) -> bool:
        return self.jsonl_path is not None or bool(self.otlp_endpoint)

    def export(self, summary: TraceSummary, events: list[TraceEvent]) -> dict[str, Any] | None:
        """Build and emit OTLP payload; failures are logged and never break agent execution."""
        if not self.enabled:
            return None

        payload = build_otel_spans(summary, events, service_name=self.service_name)
        if self.jsonl_path is not None:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

        if self.otlp_endpoint:
            try:
                httpx.post(self.otlp_endpoint, json=payload, timeout=3.0)
            except httpx.HTTPError as exc:
                logger.warning("OTLP span export failed (ignored): %s", exc)

        return payload
