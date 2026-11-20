"""Phase 1 live demonstration: Native multi-step Agent Loop on local Ollama."""

from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from enterprise_ops_agent.config import get_settings
from enterprise_ops_agent.llm import LLMClient
from enterprise_ops_agent.runtime import AgentConfig, AgentEngine
from enterprise_ops_agent.tools.mock_ops import build_mock_ops_registry

console = Console()

SYSTEM_PROMPT = """你是企业内部 IT 与运维排障助手（Enterprise Ops Copilot）。
工作准则：
1. 必须先通过工具获取真实工单、监控指标与发布记录，严禁猜测或编造数字。
2. 遇到下游依赖异常时，继续顺藤摸瓜查询下游服务的指标，直到定位完整因果链。
3. 最终总结需清晰包含：受影响工单、关键指标证据、最可能的根因、以及建议下一步操作。"""


async def run_demo(query: str, model: str | None) -> None:
    settings = get_settings()
    target_model = model or settings.llm_model
    registry = build_mock_ops_registry()
    engine = AgentEngine(
        llm_client=LLMClient(settings),
        tool_registry=registry,
        config=AgentConfig(
            max_steps=8,
            max_total_tokens=24000,
            max_context_tokens=12000,
            max_consecutive_duplicates=2,
        ),
    )

    console.print(Panel(f"[bold]Query:[/bold] {query}\n[bold]Model:[/bold] {target_model}"))
    result = await engine.run(query, system_prompt=SYSTEM_PROMPT, model=target_model)

    step_table = Table(title=f"Execution Trace (run_id={result.run_id})")
    step_table.add_column("Step", justify="right")
    step_table.add_column("Latency (ms)", justify="right")
    step_table.add_column("Tokens (in/out)", justify="right")
    step_table.add_column("Pruned")
    step_table.add_column("Tool Calls / Outcome")

    for step in result.steps:
        if step.tool_calls:
            calls_desc = "\n".join(
                f"{tc.tool_name}({tc.arguments}) -> {'OK' if tc.ok else tc.error_code}"
                for tc in step.tool_calls
            )
        else:
            calls_desc = f"[green]FINAL ANSWER[/green]: {step.assistant_preview}"
        step_table.add_row(
            str(step.step_index),
            f"{step.latency_ms:.1f}",
            f"{step.prompt_tokens}/{step.completion_tokens}",
            "yes" if step.pruned_context else "no",
            calls_desc,
        )

    console.print(step_table)
    console.print(
        f"[bold]StopReason:[/bold] {result.stop_reason.value} | "
        f"[bold]Steps:[/bold] {result.steps_taken} | "
        f"[bold]Total Tokens:[/bold] {result.total_tokens} | "
        f"[bold]Total Latency:[/bold] {result.total_latency_ms:.1f} ms"
    )
    console.print(Panel(result.final_answer, title="Final Answer", border_style="green"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 1 native agent loop demo.")
    parser.add_argument(
        "--query",
        default="请排查当前所有 open 状态的故障工单，顺藤摸瓜检查相关服务和下游依赖的实时指标及最近24小时发布记录，然后给出根因分析与处理建议。",
    )
    parser.add_argument("--model", default=None, help="Override LLM_MODEL from settings.")
    args = parser.parse_args()
    asyncio.run(run_demo(args.query, args.model))


if __name__ == "__main__":
    main()
