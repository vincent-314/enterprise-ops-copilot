"""Rich terminal renderer for chronological AuditTracer replay."""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from enterprise_ops_agent.observability.tracer import AuditTracer


def render_trace(tracer: AuditTracer, trace_id: str, console: Console | None = None) -> None:
    """Print trace summary and ordered audit events for a given trace_id."""
    out = console or Console()
    summary = tracer.get_summary(trace_id)
    if summary is None:
        out.print(f"[red]Trace '{trace_id}' not found.[/red]")
        return

    events = tracer.get_events(trace_id)
    out.print(
        Panel(
            Text(
                f"trace_id={summary.trace_id} | run_id={summary.run_id}\n"
                f"user={summary.user_id} (role={summary.user_role}) | model={summary.model}\n"
                f"status={summary.status} | stop_reason={summary.stop_reason}\n"
                f"total_tokens={summary.total_tokens} | total_latency_ms={summary.total_latency_ms}\n"
                f"security_events={summary.security_events}"
            ),
            title="Audit Trace Summary",
            border_style="cyan",
        )
    )

    table = Table(title=f"Chronological Audit Events ({len(events)})")
    table.add_column("#", justify="right")
    table.add_column("Step", justify="right")
    table.add_column("Event Type", style="bold")
    table.add_column("Payload")

    for idx, ev in enumerate(events, start=1):
        table.add_row(
            str(idx),
            str(ev.step_index),
            ev.event_type,
            Text(json.dumps(ev.payload, ensure_ascii=False)),
        )
    out.print(table)
