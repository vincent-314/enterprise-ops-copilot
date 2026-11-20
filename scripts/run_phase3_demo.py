"""Phase 3 live demo: MCP stdio Ops server + ACL RAG + HITL Pause/Resume."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from enterprise_ops_agent.config import get_settings
from enterprise_ops_agent.llm import LLMClient
from enterprise_ops_agent.mcp_bridge import MCPToolSession
from enterprise_ops_agent.ops_backend import OpsStore
from enterprise_ops_agent.rag import KnowledgeStore, register_knowledge_tool
from enterprise_ops_agent.runtime import (
    AgentConfig,
    AgentEngine,
    AgentRunResult,
    CheckpointStore,
    StopReason,
)
from enterprise_ops_agent.tools import ToolRegistry, UserContext

console = Console()

SYSTEM_PROMPT = """你是企业级内部运维排障与变更助手（Enterprise Ops Copilot）。
工作准则：
1. 先用只读工具排查：查看 open 工单、检查受影响服务及下游依赖指标、检索最近发布记录、并结合 `search_internal_knowledge` 核对操作手册。
2. 禁止猜测版本号或服务名；所有版本号、指标必须来自工具真实返回。
3. 当定位到明确需要回滚或重启的变更时，发起对应高危工具调用（系统会自动进入人工审批拦截流程）。
4. 若审批被驳回，务必认真阅读审批人的驳回理由，并按要求调整方案（例如改为创建跟踪工单或进一步排查）。"""

DEFAULT_QUERY = (
    "请排查当前 open 的 P1 故障工单，核对服务指标、最近发布记录以及运维 Runbook。"
    "如果确认是最新发布的版本引入了问题，请发起将该服务回滚到上一稳定版本的操作。"
)


def _print_trace(result: AgentRunResult, title: str) -> None:
    table = Table(title=title)
    table.add_column("Step", justify="right")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Tokens (in/out)", justify="right")
    table.add_column("Outcome")
    for step in result.steps:
        desc = (
            "\n".join(
                f"{tc.tool_name}({tc.arguments}) -> {'OK' if tc.ok else tc.error_code}"
                for tc in step.tool_calls
            )
            if step.tool_calls
            else step.assistant_preview
        )
        table.add_row(
            str(step.step_index),
            f"{step.latency_ms:.1f}",
            f"{step.prompt_tokens}/{step.completion_tokens}",
            desc,
        )
    console.print(table)


async def run_demo(decision_mode: str, reset_db: bool) -> None:
    settings = get_settings()
    ops_db_path = Path("data/phase3_demo_ops.db")
    ckpt_db_path = Path("data/phase3_demo_checkpoints.db")
    if reset_db:
        ops_db_path.unlink(missing_ok=True)
        ckpt_db_path.unlink(missing_ok=True)

    rag_db_path = Path(settings.rag_db_path)
    if not rag_db_path.exists():
        raise FileNotFoundError(
            f"{rag_db_path} not found. Run `uv run python scripts/ingest_knowledge.py` first."
        )

    client = LLMClient(settings)
    knowledge_store = KnowledgeStore(rag_db_path)
    ckpt_store = CheckpointStore(ckpt_db_path)
    registry = ToolRegistry()
    register_knowledge_tool(
        registry,
        knowledge_store,
        client,
        max_context_chars=settings.rag_max_context_chars,
    )

    caller = UserContext(user_id="bob", role="ops_admin", department="sre_ops")

    async with MCPToolSession(env={"OPS_DB_PATH": str(ops_db_path)}) as mcp_session:
        mcp_tools = await mcp_session.register_tools(registry)
        console.print(
            Panel(
                f"[bold]MCP Tools Discovered:[/bold] {', '.join(mcp_tools)}\n"
                f"[bold]Caller:[/bold] {caller.user_id} (role={caller.role})\n"
                f"[bold]Query:[/bold] {DEFAULT_QUERY}",
                title="Phase 3: MCP + ACL RAG + HITL",
            )
        )

        engine = AgentEngine(
            llm_client=client,
            tool_registry=registry,
            config=AgentConfig(max_steps=8, max_total_tokens=32000, max_context_tokens=12000),
            checkpoint_store=ckpt_store,
        )

        result = await engine.run(
            DEFAULT_QUERY,
            system_prompt=SYSTEM_PROMPT,
            model=settings.llm_model,
            user_context=caller,
        )

        while result.stop_reason == StopReason.AWAITING_APPROVAL and result.checkpoint_id:
            pending = result.pending_approval
            console.print(
                Panel(
                    f"[bold yellow]HITL 审批拦截触发！[/bold yellow]\n"
                    f"Checkpoint ID: [bold]{result.checkpoint_id}[/bold]\n"
                    f"高危工具: [bold]{pending.tool_name if pending else '?'}[/bold]\n"
                    f"参数: {pending.arguments if pending else {}}\n"
                    f"风险等级: {pending.risk_level if pending else 'high_risk'}",
                    border_style="yellow",
                )
            )

            if decision_mode == "approve":
                approved = True
                comment = "值班主管审批通过：确认回滚至稳定版本。"
            elif decision_mode == "reject":
                approved = False
                comment = "审批驳回：暂不直接回滚，请先创建一个 P1 跟踪工单并通知值班研发。"
            else:
                ans = input("是否批准该高危操作？[y/N]: ").strip().lower()
                approved = ans in {"y", "yes"}
                comment = input("请输入审批备注（可留空）: ").strip()

            console.print(
                f"[cyan]Resuming checkpoint {result.checkpoint_id} -> "
                f"approved={approved} ({comment})[/cyan]"
            )
            result = await engine.resume(
                result.checkpoint_id,
                approved=approved,
                reviewer="sre_lead_zhang",
                comment=comment,
            )

        _print_trace(result, f"Phase 3 Trace (run_id={result.run_id})")
        console.print(
            f"[bold]StopReason:[/bold] {result.stop_reason.value} | "
            f"[bold]Steps:[/bold] {result.steps_taken} | "
            f"[bold]Total Tokens:[/bold] {result.total_tokens} | "
            f"[bold]Total Latency:[/bold] {result.total_latency_ms:.1f} ms"
        )
        console.print(Panel(result.final_answer, title="Final Answer", border_style="green"))

    order_state = OpsStore(ops_db_path).get_service_metrics("order-api")
    open_tickets = OpsStore(ops_db_path).list_incidents("open")
    console.print(
        Panel(
            f"order-api 当前版本: [bold]{order_state['current_version']}[/bold] | "
            f"状态: [bold]{order_state['status']}[/bold]\n"
            f"当前 open 工单数: [bold]{len(open_tickets)}[/bold]",
            title="Persisted Ops DB Post-Check",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 3 MCP + HITL live demo.")
    parser.add_argument(
        "--decision",
        choices=["prompt", "approve", "reject"],
        default="prompt",
        help="How to handle HITL approval gate.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Reuse existing phase3 demo SQLite files instead of resetting.",
    )
    args = parser.parse_args()
    asyncio.run(run_demo(args.decision, reset_db=not args.no_reset))


if __name__ == "__main__":
    main()
