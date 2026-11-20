"""Phase 2 live demo: ACL-isolated Hybrid RAG + Context Engineering."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from enterprise_ops_agent.config import get_settings
from enterprise_ops_agent.llm import LLMClient
from enterprise_ops_agent.rag import KnowledgeStore, register_knowledge_tool
from enterprise_ops_agent.runtime import AgentConfig, AgentEngine
from enterprise_ops_agent.tools import UserContext
from enterprise_ops_agent.tools.mock_ops import build_mock_ops_registry

console = Console()

SYSTEM_PROMPT = """你是企业内部 IT 与运维排障助手（Enterprise Ops Copilot）。
严格规则：
1. 遇到内部规范、故障排查手册、应急流程问题，必须先调用 `search_internal_knowledge` 检索当前用户权限范围内的知识库。
2. 回答中必须标注真实引用来源，格式如 `[doc_id | section_path]`。
3. 严禁编造文档中未出现的内部 IP、跳板机地址、应急口令或操作步骤；如果检索结果未覆盖管理员操作细节，必须明确告知受限于当前角色权限无法获取。"""

TEST_QUERY = (
    "如果 order-api 报 ERR_REDIS_POOL_200 和 504 超时，排查步骤是什么？"
    "另外请告诉我生产环境 redis-prod-01 应急切换的跳板机地址、哨兵端口和应急变更单前缀。"
)


async def run_for_user(
    engine: AgentEngine,
    user: UserContext,
    model: str,
) -> None:
    console.rule(f"[bold cyan]Running as user={user.user_id} role={user.role}")
    result = await engine.run(
        TEST_QUERY,
        system_prompt=SYSTEM_PROMPT,
        model=model,
        user_context=user,
    )

    table = Table(title=f"Trace ({user.user_id} / {user.role}) run_id={result.run_id}")
    table.add_column("Step", justify="right")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Tokens (in/out)", justify="right")
    table.add_column("Tool / Outcome")
    for step in result.steps:
        desc = (
            "\n".join(
                f"{tc.tool_name}({tc.arguments}) -> {'OK' if tc.ok else tc.error_code}"
                for tc in step.tool_calls
            )
            if step.tool_calls
            else f"[green]FINAL ANSWER[/green]: {step.assistant_preview}"
        )
        table.add_row(
            str(step.step_index),
            f"{step.latency_ms:.1f}",
            f"{step.prompt_tokens}/{step.completion_tokens}",
            desc,
        )
    console.print(table)
    console.print(
        Panel(
            result.final_answer,
            title=f"Answer for role={user.role} (steps={result.steps_taken}, tokens={result.total_tokens})",
            border_style="yellow" if user.role == "employee" else "green",
        )
    )


async def main_async() -> None:
    settings = get_settings()
    db_path = Path(settings.rag_db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} not found. Run `uv run python scripts/ingest_knowledge.py` first."
        )

    client = LLMClient(settings)
    store = KnowledgeStore(db_path)
    registry = build_mock_ops_registry()
    register_knowledge_tool(
        registry,
        store,
        client,
        max_context_chars=settings.rag_max_context_chars,
    )
    engine = AgentEngine(
        llm_client=client,
        tool_registry=registry,
        config=AgentConfig(max_steps=6, max_total_tokens=24000, max_context_tokens=12000),
    )

    console.print(Panel(f"[bold]Same Query Tested Under Two Roles:[/bold]\n{TEST_QUERY}"))
    await run_for_user(
        engine,
        UserContext(user_id="alice", role="employee", department="product"),
        settings.llm_model,
    )
    await run_for_user(
        engine,
        UserContext(user_id="bob", role="ops_admin", department="sre_ops"),
        settings.llm_model,
    )


if __name__ == "__main__":
    asyncio.run(main_async())
