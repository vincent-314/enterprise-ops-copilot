"""Phase 5 Eval Benchmark CLI: compare qwen3:8b, qwen3:14b, and ComplexityRouter."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

from enterprise_ops_agent.config import get_settings
from enterprise_ops_agent.evals import (
    CaseEvalResult,
    EvalRunner,
    SuiteReport,
    build_golden_eval_cases,
)
from enterprise_ops_agent.llm import LLMClient
from enterprise_ops_agent.runtime import ComplexityRouter
from enterprise_ops_agent.storage import create_knowledge_store

console = Console()


def _render_report_table(reports: list[SuiteReport]) -> None:
    summary = Table(title="Phase 5 Eval Benchmark Comparison")
    summary.add_column("Strategy", style="bold")
    summary.add_column("Det Pass", justify="right")
    summary.add_column("Overall Pass", justify="right")
    summary.add_column("Pass Rate", justify="right")
    summary.add_column("Avg Faithfulness", justify="right")
    summary.add_column("P50 Latency (ms)", justify="right")
    summary.add_column("P95 Latency (ms)", justify="right")
    summary.add_column("Total Tokens", justify="right")
    summary.add_column("Models Used")

    for rep in reports:
        faith = f"{rep.avg_faithfulness:.2f}/5" if rep.avg_faithfulness is not None else "n/a"
        models_str = ", ".join(f"{m}:{cnt}" for m, cnt in sorted(rep.model_distribution.items()))
        summary.add_row(
            rep.strategy_name,
            f"{rep.deterministic_passed}/{rep.total_cases}",
            f"{rep.overall_passed}/{rep.total_cases}",
            f"{rep.pass_rate_pct:.1f}%",
            faith,
            f"{rep.p50_latency_ms:.1f}",
            f"{rep.p95_latency_ms:.1f}",
            str(rep.total_tokens),
            models_str,
        )
    console.print(summary)

    for rep in reports:
        detail = Table(title=f"Case Breakdown - {rep.strategy_name}")
        detail.add_column("Case ID", style="bold")
        detail.add_column("Model")
        detail.add_column("Result")
        detail.add_column("Latency (ms)", justify="right")
        detail.add_column("Tokens", justify="right")
        detail.add_column("Tools")
        detail.add_column("Notes / Failures")

        for item in rep.results:
            status = "[green]PASS[/green]" if item.overall_passed else "[red]FAIL[/red]"
            notes: list[str] = list(item.failures)
            if item.judge_verdict is not None:
                notes.append(
                    f"Judge(faith={item.judge_verdict.faithfulness}, "
                    f"policy={item.judge_verdict.policy_compliance}): {item.judge_verdict.critique}"
                )
            detail.add_row(
                item.case_id,
                item.routed_model,
                status,
                f"{item.total_latency_ms:.1f}",
                str(item.total_tokens),
                ", ".join(item.tools_called) or "-",
                " | ".join(notes) or "OK",
            )
        console.print(detail)


def _write_markdown_report(reports: list[SuiteReport], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Phase 5 Automated Eval Benchmark Report\n",
        "## Summary\n",
        "| Strategy | Det Pass | Overall Pass | Pass Rate | Avg Faithfulness | P50 (ms) | P95 (ms) | Total Tokens | Models |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for rep in reports:
        faith = f"{rep.avg_faithfulness:.2f}/5" if rep.avg_faithfulness is not None else "n/a"
        models_str = ", ".join(f"{m}:{cnt}" for m, cnt in sorted(rep.model_distribution.items()))
        lines.append(
            f"| {rep.strategy_name} | {rep.deterministic_passed}/{rep.total_cases} | "
            f"{rep.overall_passed}/{rep.total_cases} | {rep.pass_rate_pct:.1f}% | "
            f"{faith} | {rep.p50_latency_ms:.1f} | {rep.p95_latency_ms:.1f} | "
            f"{rep.total_tokens} | {models_str} |"
        )

    for rep in reports:
        lines.append(f"\n## Case Details: {rep.strategy_name}\n")
        lines.append("| Case ID | Model | Status | Latency (ms) | Tokens | Tools | Detail |")
        lines.append("| :--- | :--- | :---: | ---: | ---: | :--- | :--- |")
        for item in rep.results:
            status = "PASS" if item.overall_passed else "FAIL"
            notes = list(item.failures)
            if item.judge_verdict is not None:
                notes.append(
                    f"Judge({item.judge_verdict.faithfulness}/{item.judge_verdict.policy_compliance}): "
                    f"{item.judge_verdict.critique}"
                )
            note_str = " ; ".join(notes).replace("|", "\\|") or "OK"
            tools_str = ", ".join(item.tools_called) or "-"
            lines.append(
                f"| {item.case_id} | {item.routed_model} | {status} | "
                f"{item.total_latency_ms:.1f} | {item.total_tokens} | {tools_str} | {note_str} |"
            )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main_async(
    mode: str,
    use_judge: bool,
    case_filter: set[str] | None,
    output_path: Path,
) -> None:
    settings = get_settings()
    if settings.storage_backend == "sqlite":
        rag_db_path = Path(settings.rag_db_path)
        if not rag_db_path.exists():
            raise FileNotFoundError(
                f"{rag_db_path} not found. Run `uv run python scripts/ingest_knowledge.py` first."
            )

    all_cases = build_golden_eval_cases(locale=settings.app_locale)
    cases = [c for c in all_cases if not case_filter or c.case_id in case_filter]
    if not cases:
        raise ValueError(f"No eval cases matched filter: {case_filter}")

    client = LLMClient(settings)
    knowledge_store = create_knowledge_store(settings)
    runner = EvalRunner(
        settings=settings,
        llm_client=client,
        knowledge_store=knowledge_store,
        embedder=client,
        judge_llm=client if use_judge else None,
        judge_model=settings.router_strong_model if use_judge else None,
    )

    def _log_progress(item: CaseEvalResult) -> None:
        tag = "[green]PASS[/green]" if item.overall_passed else "[red]FAIL[/red]"
        console.print(
            f"  -> {item.case_id} ({item.routed_model}): {tag} "
            f"[{item.total_latency_ms:.0f}ms, {item.total_tokens} tok]"
        )

    reports: list[SuiteReport] = []
    if mode in ("8b", "compare-all"):
        console.rule(f"[bold cyan]Running Eval Suite: fixed {settings.router_fast_model}")
        reports.append(
            await runner.run_suite(
                cases,
                strategy_name=f"fixed:{settings.router_fast_model}",
                fixed_model=settings.router_fast_model,
                on_case_done=_log_progress,
            )
        )
    if mode in ("14b", "compare-all"):
        console.rule(f"[bold cyan]Running Eval Suite: fixed {settings.router_strong_model}")
        reports.append(
            await runner.run_suite(
                cases,
                strategy_name=f"fixed:{settings.router_strong_model}",
                fixed_model=settings.router_strong_model,
                on_case_done=_log_progress,
            )
        )
    if mode in ("router", "compare-all"):
        console.rule("[bold cyan]Running Eval Suite: ComplexityRouter (8b + 14b)")
        router = ComplexityRouter(
            client,
            fast_model=settings.router_fast_model,
            strong_model=settings.router_strong_model,
        )
        reports.append(
            await runner.run_suite(
                cases,
                strategy_name="router:8b+14b",
                router=router,
                on_case_done=_log_progress,
            )
        )

    _render_report_table(reports)
    _write_markdown_report(reports, output_path)
    console.print(f"\n[green]Saved eval report to {output_path}[/green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 5 enterprise agent eval benchmarks.")
    parser.add_argument(
        "--mode",
        choices=["8b", "14b", "router", "compare-all"],
        default="compare-all",
    )
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM-as-a-Judge grading.")
    parser.add_argument("--cases", default="", help="Optional comma-separated case IDs.")
    parser.add_argument("--output", default="reports/phase5_eval_report.md")
    args = parser.parse_args()

    case_filter = {c.strip() for c in args.cases.split(",") if c.strip()} or None
    asyncio.run(
        main_async(
            mode=args.mode,
            use_judge=not args.no_judge,
            case_filter=case_filter,
            output_path=Path(args.output),
        )
    )


if __name__ == "__main__":
    main()
