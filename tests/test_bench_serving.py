"""Unit tests for the Week 3 LLM serving benchmark harness."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bench_serving as bs  # noqa: E402


def test_percentile_and_summary_aggregation() -> None:
    assert bs.percentile([], 95) == 0.0
    assert bs.percentile([42.0], 95) == 42.0
    assert round(bs.percentile([10.0, 20.0, 30.0, 40.0], 50), 2) == 25.0

    samples = [
        bs.RequestSample("short_triage", 4, True, 100.0, 10.0, 500.0, 120, 40, True),
        bs.RequestSample("short_triage", 4, True, 200.0, 15.0, 800.0, 140, 60, True),
        bs.RequestSample("short_triage", 4, False, 0.0, 0.0, 1000.0, 0, 0, False, "timeout"),
    ]
    summary = bs.build_stage_summary("short_triage", 4, samples, wall_time_s=2.0)
    assert summary.ok_requests == 2
    assert summary.total_requests == 3
    assert summary.rps == 1.0
    assert summary.system_throughput_tok_s == 50.0
    assert summary.ttft_p50_ms == 150.0
    assert summary.valid_rate == round(2 / 3, 4)


def test_prometheus_metrics_parser_and_delta() -> None:
    raw = """
# HELP vllm:kv_cache_usage_perc KV-cache usage
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen3-8B"} 0.375
vllm:prefix_cache_hits_total{engine="0"} 12000
vllm:prefix_cache_queries_total{engine="0"} 16000
vllm:num_requests_waiting{engine="0"} 2
"""
    before = {"vllm:prefix_cache_hits_total": 2000.0, "vllm:prefix_cache_queries_total": 4000.0}
    after = bs.parse_prometheus_metrics(raw)
    assert after["vllm:kv_cache_usage_perc"] == 0.375

    delta = bs.compute_metrics_delta(before, after)
    assert delta["vllm:prefix_cache_hits_total_delta"] == 10000.0
    assert delta["vllm:prefix_cache_queries_total_delta"] == 12000.0
    assert delta["prefix_cache_hit_rate"] == round(10000.0 / 12000.0, 4)


def test_output_validators_for_structured_and_tool_workloads() -> None:
    valid_json = json.dumps(
        {
            "category": "database",
            "severity": "P1",
            "requires_escalation": True,
            "summary": "Primary MySQL pool exhausted causing checkout timeouts.",
        }
    )
    assert bs.validate_workload_output("short_triage", f"```json\n{valid_json}\n```", {}, {})
    assert not bs.validate_workload_output("short_triage", '{"category": "invalid"}', {}, {})

    tool_map = {
        0: {
            "name": "query_service_health",
            "arguments": '{"service_name": "redis-prod-01", "include_connections": true}',
        }
    }
    assert bs.validate_workload_output(
        "tool_call_burst",
        "",
        tool_map,
        {"_expected_service": "redis-prod-01"},
    )
    assert not bs.validate_workload_output(
        "tool_call_burst",
        "",
        tool_map,
        {"_expected_service": "kafka-broker-03"},
    )


def test_render_markdown_report_contains_table_row() -> None:
    summary = bs.StageSummary(
        workload="prefix_cache_rag_warm",
        concurrency=8,
        total_requests=8,
        ok_requests=8,
        wall_time_s=3.2,
        rps=2.5,
        system_throughput_tok_s=320.5,
        ttft_p50_ms=45.2,
        ttft_p95_ms=82.0,
        tpot_p50_ms=11.4,
        tpot_p95_ms=14.9,
        e2e_p50_ms=980.0,
        e2e_p95_ms=1320.0,
        valid_rate=1.0,
        avg_prompt_tokens=1950.0,
        avg_completion_tokens=128.2,
    )
    md = bs.render_markdown_report(
        {
            "timestamp_utc": "2026-11-21T00:00:00Z",
            "engine": "vllm",
            "model": "Qwen/Qwen3-8B",
            "base_url": "http://127.0.0.1:8000/v1",
            "gpu_name": "NVIDIA L4",
        },
        [summary],
    )
    assert "`prefix_cache_rag_warm`" in md
    assert "320.5" in md
    assert "100.0%" in md
