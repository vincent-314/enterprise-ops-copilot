"""Integration and unit tests for MCP stdio bridge and HITL pause/resume checkpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from enterprise_ops_agent.llm.client import LLMResponse, ToolCallRequest
from enterprise_ops_agent.mcp_bridge import MCPToolSession
from enterprise_ops_agent.ops_backend import OpsStore
from enterprise_ops_agent.runtime import (
    AgentConfig,
    AgentEngine,
    CheckpointStore,
    StopReason,
)
from enterprise_ops_agent.tools import RiskLevel, ToolRegistry, UserContext


class ScriptedLLM:
    """Deterministic LLM double capturing prompts across pause and resume boundaries."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del tools, tool_choice, response_format, temperature, max_tokens, model, extra_body
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of responses.")
        return self._responses.pop(0)


async def test_mcp_stdio_discovery_risk_mapping_and_mutation(tmp_path: Path) -> None:
    ops_db = tmp_path / "ops.db"
    registry = ToolRegistry()

    async with MCPToolSession(env={"OPS_DB_PATH": str(ops_db)}) as mcp_session:
        registered = await mcp_session.register_tools(registry)
        assert set(registered) == {
            "list_incidents",
            "get_service_metrics",
            "search_recent_deployments",
            "create_ticket",
            "restart_service",
            "rollback_deployment",
        }

        assert registry.get("list_incidents").risk_level == RiskLevel.READ
        assert registry.get("create_ticket").risk_level == RiskLevel.WRITE_LOW
        assert registry.get("restart_service").risk_level == RiskLevel.HIGH_RISK
        assert registry.get("rollback_deployment").risk_level == RiskLevel.HIGH_RISK

        incidents_res = await registry.execute("list_incidents", {"status": "open"})
        assert incidents_res.ok
        assert json.loads(incidents_res.output)["count"] == 1

        rollback_res = await registry.execute(
            "rollback_deployment",
            {
                "service_name": "order-api",
                "target_version": "v2.14.2",
                "reason": "v2.14.3 RedisPool leak",
            },
        )
        assert rollback_res.ok
        payload = json.loads(rollback_res.output)
        assert payload["current_version"] == "v2.14.2"

    # Verify persistence after MCP subprocess closed.
    store = OpsStore(ops_db)
    svc = store.get_service_metrics("order-api")
    assert svc is not None
    assert svc["current_version"] == "v2.14.2"
    assert svc["status"] == "healthy"


async def test_hitl_pause_and_approve_resume(tmp_path: Path) -> None:
    ops_db = tmp_path / "ops.db"
    ckpt_db = tmp_path / "checkpoints.db"
    ckpt_store = CheckpointStore(ckpt_db)
    registry = ToolRegistry()

    async with MCPToolSession(env={"OPS_DB_PATH": str(ops_db)}) as mcp_session:
        await mcp_session.register_tools(registry)

        llm = ScriptedLLM(
            [
                # Step 1: read-only inspection
                LLMResponse(
                    model="fake",
                    prompt_tokens=80,
                    completion_tokens=20,
                    tool_calls=[
                        ToolCallRequest(
                            id="c1",
                            name="list_incidents",
                            arguments_raw='{"status": "open"}',
                            arguments={"status": "open"},
                        )
                    ],
                ),
                # Step 2: high-risk rollback -> should pause for HITL
                LLMResponse(
                    model="fake",
                    prompt_tokens=120,
                    completion_tokens=25,
                    tool_calls=[
                        ToolCallRequest(
                            id="c2",
                            name="rollback_deployment",
                            arguments_raw='{"service_name": "order-api", "target_version": "v2.14.2", "reason": "连接泄漏"}',
                            arguments={
                                "service_name": "order-api",
                                "target_version": "v2.14.2",
                                "reason": "连接泄漏",
                            },
                        )
                    ],
                ),
                # Step 3 (after resume): final answer summarizing rollback result
                LLMResponse(
                    model="fake",
                    content="已获审批并完成 order-api 回滚至 v2.14.2，服务已恢复健康。",
                    prompt_tokens=160,
                    completion_tokens=30,
                ),
            ]
        )

        engine = AgentEngine(
            llm,
            registry,
            config=AgentConfig(max_steps=6),
            checkpoint_store=ckpt_store,
        )
        caller = UserContext(user_id="bob", role="ops_admin", department="sre_ops")
        paused = await engine.run(
            "排查并回滚 order-api",
            system_prompt="你是运维助手",
            user_context=caller,
        )

        assert paused.stop_reason == StopReason.AWAITING_APPROVAL
        assert paused.checkpoint_id is not None
        assert paused.pending_approval is not None
        assert paused.pending_approval.tool_name == "rollback_deployment"
        assert len(ckpt_store.list_pending()) == 1

        # High-risk mutation must NOT have executed while awaiting approval.
        assert OpsStore(ops_db).get_service_metrics("order-api")["current_version"] == "v2.14.3"

        resumed = await engine.resume(
            paused.checkpoint_id,
            approved=True,
            reviewer="sre_lead",
            comment="同意回滚至稳定版 v2.14.2",
        )
        assert resumed.stop_reason == StopReason.COMPLETED
        assert "v2.14.2" in resumed.final_answer
        assert OpsStore(ops_db).get_service_metrics("order-api")["current_version"] == "v2.14.2"
        assert len(ckpt_store.list_pending()) == 0

        # Replay of already-approved checkpoint must fail.
        with pytest.raises(ValueError, match="already resolved"):
            await engine.resume(paused.checkpoint_id, approved=True, reviewer="attacker")


async def test_hitl_reject_feeds_reason_back_to_agent(tmp_path: Path) -> None:
    ops_db = tmp_path / "ops.db"
    ckpt_store = CheckpointStore(tmp_path / "checkpoints.db")
    registry = ToolRegistry()

    async with MCPToolSession(env={"OPS_DB_PATH": str(ops_db)}) as mcp_session:
        await mcp_session.register_tools(registry)

        llm = ScriptedLLM(
            [
                # Step 1: requests high-risk restart
                LLMResponse(
                    model="fake",
                    prompt_tokens=80,
                    completion_tokens=20,
                    tool_calls=[
                        ToolCallRequest(
                            id="c1",
                            name="restart_service",
                            arguments_raw='{"service_name": "redis-prod-01", "reason": "连接数满"}',
                            arguments={"service_name": "redis-prod-01", "reason": "连接数满"},
                        )
                    ],
                ),
                # Step 2 (after rejection): adapts by creating an escalation ticket instead
                LLMResponse(
                    model="fake",
                    prompt_tokens=140,
                    completion_tokens=30,
                    tool_calls=[
                        ToolCallRequest(
                            id="c2",
                            name="create_ticket",
                            arguments_raw='{"title": "Redis连接数满跟进", "service_name": "redis-prod-01", "severity": "P1", "symptom": "按审批要求先建单排查上游"}',
                            arguments={
                                "title": "Redis连接数满跟进",
                                "service_name": "redis-prod-01",
                                "severity": "P1",
                                "symptom": "按审批要求先建单排查上游",
                            },
                        )
                    ],
                ),
                # Step 3: final answer
                LLMResponse(
                    model="fake",
                    content="重启请求已被驳回，我已按要求改为创建 P1 跟踪工单。",
                    prompt_tokens=190,
                    completion_tokens=20,
                ),
            ]
        )

        engine = AgentEngine(
            llm,
            registry,
            config=AgentConfig(max_steps=6),
            checkpoint_store=ckpt_store,
        )
        paused = await engine.run("重启 redis-prod-01", system_prompt="你是运维助手")
        assert paused.stop_reason == StopReason.AWAITING_APPROVAL
        assert paused.checkpoint_id is not None

        resumed = await engine.resume(
            paused.checkpoint_id,
            approved=False,
            reviewer="ops_director",
            comment="严禁直接重启生产 Redis，请先建单排查上游连接泄漏",
        )

        assert resumed.stop_reason == StopReason.COMPLETED
        assert "工单" in resumed.final_answer
        # Verify redis-prod-01 was NOT restarted (still degraded), while ticket WAS created.
        store = OpsStore(ops_db)
        assert store.get_service_metrics("redis-prod-01")["status"] == "degraded"
        assert len(store.list_incidents("open")) == 2
        # Verify the rejection reason reached the LLM in Step 2.
        tool_msgs = [m for m in llm.calls[1] if m.get("role") == "tool"]
        assert any("严禁直接重启生产 Redis" in m["content"] for m in tool_msgs)
