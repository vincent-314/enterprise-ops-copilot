"""Week 3 Production LLM Serving & Concurrency Benchmark Harness.

Benchmarks any OpenAI-compatible endpoint (Ollama, vLLM, SGLang) across:
1. Structured JSON output under concurrency (`short_triage`)
2. Shared-prefix RAG/Agent Cold-vs-Warm cache speedup (`prefix_cache_rag`)
3. Concurrent tool-call schema precision (`tool_call_burst`)

Collects streaming TTFT (P50/P95), TPOT/ITL (P50/P95), E2E latency (P50/P95),
system decode throughput (tok/s), RPS, schema/tool validity rates, and optional
vLLM/SGLang Prometheus KV-cache & prefix-cache `/metrics` snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.table import Table

console = Console()


class BenchTicketTriage(BaseModel):
    """Structured triage schema used to measure guided-decoding reliability."""

    category: Literal["network", "database", "auth", "compute", "other"]
    severity: Literal["P1", "P2", "P3"]
    requires_escalation: bool
    summary: str = Field(min_length=5, max_length=160)


BENCH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_service_health",
            "description": "Query live CPU, memory, and connection metrics for a named service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "Exact service identifier, e.g. redis-prod-01",
                    },
                    "include_connections": {
                        "type": "boolean",
                        "description": "Whether to include active connection count.",
                    },
                },
                "required": ["service_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_runbook",
            "description": "Search internal operations runbooks for troubleshooting steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]

_SHARED_SRE_CONTEXT = (
    "You are a Principal SRE Assistant inside Enterprise Ops Copilot.\n"
    "Follow strictly documented operational runbooks and cite exact remediation steps.\n\n"
    + "\n".join(
        f"[RUNBOOK-SECTION-{i:02d}] Service tier={tier}, owner=platform-sre, "
        f"escalation_policy=pagerduty-p1, max_rollback_window_min=15. "
        f"Procedure: Verify replica lag (<100ms), inspect connection pool saturation "
        f"(warn>80%, crit>92%), capture heap/thread dump before graceful drain, validate "
        f"canary health across zones ap-southeast-1a/1b/1c, and require HITL approval "
        f"for any stateful restart or schema rollback."
        for i, tier in enumerate(
            ["payments", "auth-gateway", "redis-cluster", "orders-db", "edge-proxy"] * 6,
            start=1,
        )
    )
)

_PROM_LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)$")

_PROM_KEYS_OF_INTEREST = (
    "vllm:kv_cache_usage_perc",
    "vllm:gpu_cache_usage_perc",
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total",
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "sglang:cache_hit_rate",
    "sglang:token_usage",
    "sglang:num_running_reqs",
    "sglang:num_waiting_reqs",
)


@dataclass
class RequestSample:
    """Single streaming request measurement."""

    workload: str
    concurrency: int
    ok: bool
    ttft_ms: float
    tpot_ms: float
    e2e_ms: float
    prompt_tokens: int
    completion_tokens: int
    valid_output: bool
    error: str | None = None


@dataclass
class StageSummary:
    """Aggregated statistics for one workload + concurrency stage."""

    workload: str
    concurrency: int
    total_requests: int
    ok_requests: int
    wall_time_s: float
    rps: float
    system_throughput_tok_s: float
    ttft_p50_ms: float
    ttft_p95_ms: float
    tpot_p50_ms: float
    tpot_p95_ms: float
    e2e_p50_ms: float
    e2e_p95_ms: float
    valid_rate: float
    avg_prompt_tokens: float
    avg_completion_tokens: float
    metrics_delta: dict[str, float] = field(default_factory=dict)


def percentile(values: list[float], p: float) -> float:
    """Compute linear-interpolated percentile p in [0, 100]; returns 0.0 when empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Extract key KV-cache and queue metrics from a Prometheus `/metrics` scrape."""
    totals: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE_RE.match(line)
        if not match:
            continue
        name, raw_val = match.groups()
        if name in _PROM_KEYS_OF_INTEREST:
            totals[name] = totals.get(name, 0.0) + float(raw_val)
    return totals


async def scrape_metrics(metrics_url: str | None) -> dict[str, float]:
    """Fetch Prometheus metrics if a metrics endpoint is configured."""
    if not metrics_url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(metrics_url)
            resp.raise_for_status()
            return parse_prometheus_metrics(resp.text)
    except httpx.HTTPError:
        return {}


def compute_metrics_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    """Compute post-stage gauges and counter deltas from Prometheus snapshots."""
    result: dict[str, float] = {}
    for key, val in after.items():
        if key.endswith("_total"):
            result[f"{key}_delta"] = round(val - before.get(key, 0.0), 4)
        else:
            result[key] = round(val, 4)
    hits = result.get("vllm:prefix_cache_hits_total_delta", 0.0)
    queries = result.get("vllm:prefix_cache_queries_total_delta", 0.0)
    if queries > 0:
        result["prefix_cache_hit_rate"] = round(hits / queries, 4)
    return result


def build_stage_summary(
    workload: str,
    concurrency: int,
    samples: list[RequestSample],
    wall_time_s: float,
    metrics_delta: dict[str, float] | None = None,
) -> StageSummary:
    """Aggregate raw request samples into latency percentiles and true wall throughput."""
    ok_samples = [s for s in samples if s.ok]
    total_completion = sum(s.completion_tokens for s in ok_samples)
    safe_wall = max(wall_time_s, 1e-6)
    ttfts = [s.ttft_ms for s in ok_samples]
    tpots = [s.tpot_ms for s in ok_samples if s.tpot_ms > 0]
    e2es = [s.e2e_ms for s in ok_samples]
    valid_count = sum(1 for s in ok_samples if s.valid_output)

    return StageSummary(
        workload=workload,
        concurrency=concurrency,
        total_requests=len(samples),
        ok_requests=len(ok_samples),
        wall_time_s=round(wall_time_s, 3),
        rps=round(len(ok_samples) / safe_wall, 3),
        system_throughput_tok_s=round(total_completion / safe_wall, 2),
        ttft_p50_ms=round(percentile(ttfts, 50), 1),
        ttft_p95_ms=round(percentile(ttfts, 95), 1),
        tpot_p50_ms=round(percentile(tpots, 50), 2),
        tpot_p95_ms=round(percentile(tpots, 95), 2),
        e2e_p50_ms=round(percentile(e2es, 50), 1),
        e2e_p95_ms=round(percentile(e2es, 95), 1),
        valid_rate=round(valid_count / len(samples), 4) if samples else 0.0,
        avg_prompt_tokens=round(
            sum(s.prompt_tokens for s in ok_samples) / len(ok_samples), 1
        )
        if ok_samples
        else 0.0,
        avg_completion_tokens=round(total_completion / len(ok_samples), 1)
        if ok_samples
        else 0.0,
        metrics_delta=metrics_delta or {},
    )


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
    return cleaned


def build_workload_request(workload: str, idx: int, cold_salt: str | None = None) -> dict[str, Any]:
    """Create chat completion parameters for a specific benchmark workload."""
    if workload == "short_triage":
        incidents = [
            "MySQL primary connection pool saturated at 99%, checkout timeouts spiking.",
            "OIDC login endpoint returning HTTP 502 after cert rotation in eu-west.",
            "Kafka broker-02 disk utilization crossed 94%, consumer lag rising.",
            "Kubernetes worker node out of ephemeral storage; pod evictions triggered.",
        ]
        schema = BenchTicketTriage.model_json_schema()
        schema["additionalProperties"] = False
        return {
            "messages": [
                {
                    "role": "system",
                    "content": "You are an IT operations triage engine. Output strictly valid JSON matching the schema.",
                },
                {"role": "user", "content": incidents[idx % len(incidents)]},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "bench_ticket_triage",
                    "strict": True,
                    "schema": schema,
                },
            },
            "max_tokens": 512,
        }

    if workload.startswith("prefix_cache_rag"):
        prefix_header = f"[COLD-RUN-NONCE={cold_salt}]\n" if cold_salt else "[WARM-CANONICAL-RUNBOOK]\n"
        return {
            "messages": [
                {"role": "system", "content": prefix_header + _SHARED_SRE_CONTEXT},
                {
                    "role": "user",
                    "content": (
                        f"Incident #{idx}: redis-cluster-{idx % 4 + 1:02d} P99 latency jumped to 180ms "
                        "with replication lag 240ms. Give a concise 3-bullet action plan."
                    ),
                },
            ],
            "max_tokens": 512,
        }

    if workload == "tool_call_burst":
        services = ["redis-prod-01", "pg-orders-primary", "auth-gateway-02", "kafka-broker-03"]
        target = services[idx % len(services)]
        return {
            "messages": [
                {
                    "role": "system",
                    "content": "You are an internal SRE copilot. Call query_service_health with exact service_name.",
                },
                {
                    "role": "user",
                    "content": f"Check CPU, memory, and connection status for {target}.",
                },
            ],
            "tools": BENCH_TOOLS,
            "tool_choice": "auto",
            "max_tokens": 512,
            "_expected_service": target,
        }

    raise ValueError(f"Unsupported workload: {workload}")


def validate_workload_output(workload: str, content: str, tool_calls_map: dict[int, dict[str, str]], meta: dict[str, Any]) -> bool:
    """Validate structured JSON or tool-call output produced under load."""
    if workload == "short_triage":
        try:
            BenchTicketTriage.model_validate_json(_strip_fences(content))
            return True
        except ValidationError:
            return False

    if workload == "tool_call_burst":
        if not tool_calls_map:
            return False
        first = tool_calls_map[min(tool_calls_map)]
        if first.get("name") != "query_service_health":
            return False
        try:
            args = json.loads(first.get("arguments", "{}"))
        except json.JSONDecodeError:
            return False
        return isinstance(args, dict) and args.get("service_name") == meta.get("_expected_service")

    return bool(content.strip())


async def run_single_stream_request(
    client: AsyncOpenAI,
    *,
    model: str,
    workload: str,
    concurrency: int,
    idx: int,
    extra_body: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    cold_salt: str | None = None,
) -> RequestSample:
    """Execute one streaming chat completion request and measure exact token timings."""
    req_spec = build_workload_request(workload, idx, cold_salt=cold_salt)
    expected_service = req_spec.pop("_expected_service", None)
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        **req_spec,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    started = time.perf_counter()
    first_token_at: float | None = None
    chunks_text: list[str] = []
    reasoning_chunks: list[str] = []
    tool_calls_acc: dict[int, dict[str, str]] = {}
    prompt_tokens = 0
    completion_tokens = 0

    try:
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage is not None:
                prompt_tokens = int(chunk.usage.prompt_tokens or 0)
                completion_tokens = int(chunk.usage.completion_tokens or 0)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            delta_content = getattr(delta, "content", None) or ""
            delta_reasoning = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or ""
            )
            delta_tools = getattr(delta, "tool_calls", None) or []

            if (delta_content or delta_reasoning or delta_tools) and first_token_at is None:
                first_token_at = time.perf_counter()

            if delta_content:
                chunks_text.append(delta_content)
            if delta_reasoning:
                reasoning_chunks.append(delta_reasoning)
            for tc in delta_tools:
                entry = tool_calls_acc.setdefault(tc.index, {"name": "", "arguments": ""})
                if tc.function:
                    if tc.function.name:
                        entry["name"] += tc.function.name
                    if tc.function.arguments:
                        entry["arguments"] += tc.function.arguments

        finished = time.perf_counter()
        e2e_ms = (finished - started) * 1000.0
        ttft_ms = ((first_token_at or finished) - started) * 1000.0
        full_content = "".join(chunks_text)
        total_generated_chars = len(full_content) + len("".join(reasoning_chunks)) + sum(
            len(v["name"]) + len(v["arguments"]) for v in tool_calls_acc.values()
        )
        if completion_tokens == 0 and total_generated_chars > 0:
            completion_tokens = max(1, total_generated_chars // 4)
        decode_ms = max(0.0, e2e_ms - ttft_ms)
        tpot_ms = decode_ms / max(completion_tokens - 1, 1) if completion_tokens > 1 else 0.0
        valid = validate_workload_output(
            workload,
            full_content,
            tool_calls_acc,
            {"_expected_service": expected_service},
        )
        return RequestSample(
            workload=workload,
            concurrency=concurrency,
            ok=True,
            ttft_ms=ttft_ms,
            tpot_ms=tpot_ms,
            e2e_ms=e2e_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            valid_output=valid,
        )
    except (OpenAIError, httpx.HTTPError, TimeoutError, ValueError) as exc:
        finished = time.perf_counter()
        return RequestSample(
            workload=workload,
            concurrency=concurrency,
            ok=False,
            ttft_ms=0.0,
            tpot_ms=0.0,
            e2e_ms=(finished - started) * 1000.0,
            prompt_tokens=0,
            completion_tokens=0,
            valid_output=False,
            error=str(exc)[:180],
        )


async def run_stage(
    client: AsyncOpenAI,
    *,
    model: str,
    workload: str,
    concurrency: int,
    num_requests: int,
    metrics_url: str | None = None,
    extra_body: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    cold_prefix: bool = False,
) -> StageSummary:
    """Run a fixed-concurrency wave and return aggregated performance + validity metrics."""
    semaphore = asyncio.Semaphore(concurrency)
    before_metrics = await scrape_metrics(metrics_url)

    async def _bounded(idx: int) -> RequestSample:
        salt = f"{uuid.uuid4().hex[:8]}-{idx}" if cold_prefix else None
        async with semaphore:
            return await run_single_stream_request(
                client,
                model=model,
                workload=workload,
                concurrency=concurrency,
                idx=idx,
                extra_body=extra_body,
                reasoning_effort=reasoning_effort,
                cold_salt=salt,
            )

    t0 = time.perf_counter()
    samples = await asyncio.gather(*(_bounded(i) for i in range(num_requests)))
    wall_time_s = time.perf_counter() - t0
    after_metrics = await scrape_metrics(metrics_url)
    return build_stage_summary(
        workload=workload,
        concurrency=concurrency,
        samples=list(samples),
        wall_time_s=wall_time_s,
        metrics_delta=compute_metrics_delta(before_metrics, after_metrics),
    )


def render_markdown_report(metadata: dict[str, Any], summaries: list[StageSummary]) -> str:
    """Format benchmark metadata and stage summaries as a Markdown report."""
    lines = [
        "# Week 3 LLM Serving & Concurrency Benchmark",
        "",
        f"- **Timestamp (UTC):** `{metadata['timestamp_utc']}`",
        f"- **Engine / Label:** `{metadata['engine']}`",
        f"- **Model:** `{metadata['model']}`",
        f"- **Base URL:** `{metadata['base_url']}`",
        f"- **GPU:** `{metadata.get('gpu_name', 'unknown')}`",
        "",
        "| Workload | Conc | OK / Total | Wall (s) | RPS | Throughput (tok/s) | TTFT P50 (ms) | TTFT P95 (ms) | TPOT P50 (ms) | E2E P95 (ms) | Valid Rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        lines.append(
            f"| `{s.workload}` | {s.concurrency} | {s.ok_requests}/{s.total_requests} | "
            f"{s.wall_time_s:.2f} | {s.rps:.2f} | {s.system_throughput_tok_s:.1f} | "
            f"{s.ttft_p50_ms:.1f} | {s.ttft_p95_ms:.1f} | {s.tpot_p50_ms:.2f} | "
            f"{s.e2e_p95_ms:.1f} | {s.valid_rate * 100:.1f}% |"
        )
    return "\n".join(lines) + "\n"


def _print_rich_table(summaries: list[StageSummary]) -> None:
    table = Table(title="LLM Serving Concurrency Benchmark")
    table.add_column("Workload", style="bold")
    table.add_column("Conc", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("RPS", justify="right")
    table.add_column("Tok/s", justify="right")
    table.add_column("TTFT P50", justify="right")
    table.add_column("TTFT P95", justify="right")
    table.add_column("TPOT P50", justify="right")
    table.add_column("Valid", justify="right")
    for s in summaries:
        table.add_row(
            s.workload,
            str(s.concurrency),
            f"{s.ok_requests}/{s.total_requests}",
            f"{s.rps:.2f}",
            f"{s.system_throughput_tok_s:.1f}",
            f"{s.ttft_p50_ms:.1f}ms",
            f"{s.ttft_p95_ms:.1f}ms",
            f"{s.tpot_p50_ms:.2f}ms",
            f"{s.valid_rate * 100:.0f}%",
        )
    console.print(table)


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """Execute warmup + selected workload matrix against the target OpenAI-compatible server."""
    client = AsyncOpenAI(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.timeout,
    )
    extra_body = json.loads(args.extra_body_json) if args.extra_body_json else None

    console.print(f"[cyan]Warming up {args.model} at {args.base_url}...[/cyan]")
    await run_single_stream_request(
        client,
        model=args.model,
        workload="short_triage",
        concurrency=1,
        idx=0,
        extra_body=extra_body,
        reasoning_effort=args.reasoning_effort,
    )

    summaries: list[StageSummary] = []
    concurrencies = [int(c.strip()) for c in args.concurrency.split(",") if c.strip()]

    for workload in args.workloads:
        if workload == "prefix_cache_rag":
            cold_n = max(4, args.requests_per_stage // 2)
            summaries.append(
                await run_stage(
                    client,
                    model=args.model,
                    workload="prefix_cache_rag_cold",
                    concurrency=min(concurrencies[0], 4),
                    num_requests=cold_n,
                    metrics_url=args.metrics_url,
                    extra_body=extra_body,
                    reasoning_effort=args.reasoning_effort,
                    cold_prefix=True,
                )
            )
            summaries.append(
                await run_stage(
                    client,
                    model=args.model,
                    workload="prefix_cache_rag_warm",
                    concurrency=min(max(concurrencies), 8),
                    num_requests=args.requests_per_stage,
                    metrics_url=args.metrics_url,
                    extra_body=extra_body,
                    reasoning_effort=args.reasoning_effort,
                    cold_prefix=False,
                )
            )
            continue

        for conc in concurrencies:
            n_req = max(conc, args.requests_per_stage)
            summaries.append(
                await run_stage(
                    client,
                    model=args.model,
                    workload=workload,
                    concurrency=conc,
                    num_requests=n_req,
                    metrics_url=args.metrics_url,
                    extra_body=extra_body,
                    reasoning_effort=args.reasoning_effort,
                )
            )

    _print_rich_table(summaries)
    metadata = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "engine": args.engine,
        "model": args.model,
        "base_url": args.base_url,
        "gpu_name": args.gpu_name,
        "extra_body": extra_body,
        "reasoning_effort": args.reasoning_effort,
    }
    payload = {
        "metadata": metadata,
        "summaries": [asdict(s) for s in summaries],
        "markdown": render_markdown_report(metadata, summaries),
    }

    if args.output_json:
        out_json = Path(args.output_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[green]Saved JSON report -> {out_json}[/green]")

    if args.output_md:
        out_md = Path(args.output_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(payload["markdown"], encoding="utf-8")
        console.print(f"[green]Saved Markdown report -> {out_md}[/green]")

    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark OpenAI-compatible LLM serving endpoints.")
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1"))
    parser.add_argument("--api-key", default=os.getenv("LLM_API_KEY", "ollama"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", "qwen3:8b"))
    parser.add_argument("--engine", default="ollama", help="Label for engine (ollama, vllm, sglang).")
    parser.add_argument("--gpu-name", default="local-gpu", help="GPU label for report metadata.")
    parser.add_argument("--concurrency", default="1,4,8", help="Comma-separated concurrency sweep.")
    parser.add_argument("--requests-per-stage", type=int, default=8)
    parser.add_argument(
        "--workloads",
        nargs="+",
        default=["short_triage", "prefix_cache_rag", "tool_call_burst"],
        choices=["short_triage", "prefix_cache_rag", "tool_call_burst"],
    )
    parser.add_argument("--metrics-url", default=None, help="Optional Prometheus /metrics URL.")
    parser.add_argument(
        "--extra-body-json",
        default=None,
        help="Optional JSON object forwarded via extra_body (e.g. disable thinking).",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        choices=["none", "low", "medium", "high"],
        help="Optional OpenAI reasoning_effort parameter (e.g. 'none' disables Ollama Qwen3 thinking).",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def main() -> None:
    asyncio.run(run_benchmark(parse_args()))


if __name__ == "__main__":
    main()
