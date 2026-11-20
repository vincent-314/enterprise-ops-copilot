"""Phase 4 audit replay utility: inspect any execution trace_id chronologically."""

from __future__ import annotations

import argparse

from rich.console import Console

from enterprise_ops_agent.config import get_settings
from enterprise_ops_agent.observability import AuditTracer
from enterprise_ops_agent.observability.replay import render_trace

console = Console()


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Inspect an audit trace by trace_id.")
    parser.add_argument("trace_id", nargs="?", help="Trace ID to inspect.")
    parser.add_argument("--latest", action="store_true", help="Inspect the latest trace.")
    parser.add_argument("--db-path", default=settings.trace_db_path)
    args = parser.parse_args()

    tracer = AuditTracer(args.db_path)
    target = args.trace_id
    if args.latest or not target:
        target = tracer.get_latest_trace_id()
        if target is None:
            console.print("[yellow]No traces found in database.[/yellow]")
            return
    render_trace(tracer, target, console=console)


if __name__ == "__main__":
    main()
