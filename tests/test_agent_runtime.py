"""Offline unit tests for ToolRegistry, context pruning, and AgentEngine."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from enterprise_ops_agent.llm.client import LLMResponse, ToolCallRequest
from enterprise_ops_agent.runtime.context import prune_messages
from enterprise_ops_agent.runtime.engine import AgentConfig, AgentEngine
from enterprise_ops_agent.runtime.types import StopReason
from enterprise_ops_agent.tools.registry import ToolRegistry


class FakeLLMClient:
    """Deterministic scripted LLM fake for testing AgentEngine without network."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.recorded_messages: list[list[dict[str, Any]]] = []

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
        self.recorded_messages.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("FakeLLMClient ran out of scripted responses.")
        return self._responses.pop(0)


class LookupArgs(BaseModel):
    service_name: str = Field(min_length=2)


def _build_test_registry() -> ToolRegistry:
    registry = ToolRegistry(max_output_chars=120)

    def lookup_handler(args: LookupArgs) -> str:
        return json.dumps({"service": args.service_name, "status": "healthy"})

    async def async_fail(_args: LookupArgs) -> str:
        raise RuntimeError("downstream timeout")

    def huge_output(_args: LookupArgs) -> str:
        return "x" * 300

    registry.register("lookup_service", "Look up service status.", LookupArgs, lookup_handler)
    registry.register("failing_tool", "Always fails.", LookupArgs, async_fail)
    registry.register("huge_tool", "Returns oversized text.", LookupArgs, huge_output)
    return registry


async def test_registry_validation_and_truncation() -> None:
    registry = _build_test_registry()

    unknown = await registry.execute("missing_tool", {})
    assert not unknown.ok
    assert unknown.error_code == "UNKNOWN_TOOL"

    invalid = await registry.execute("lookup_service", {"service_name": "a"})
    assert not invalid.ok
    assert invalid.error_code == "INVALID_ARGUMENTS"

    failed = await registry.execute("failing_tool", {"service_name": "order-api"})
    assert not failed.ok
    assert failed.error_code == "TOOL_EXECUTION_ERROR"

    huge = await registry.execute("huge_tool", {"service_name": "order-api"})
    assert huge.ok
    assert huge.truncated
    assert "truncated" in huge.output


async def test_agent_engine_happy_path_and_self_correction() -> None:
    registry = _build_test_registry()
    fake = FakeLLMClient(
        [
            # Step 1: malformed arguments -> should receive INVALID_ARGUMENTS feedback
            LLMResponse(
                model="fake",
                prompt_tokens=100,
                completion_tokens=20,
                latency_ms=10.0,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="lookup_service",
                        arguments_raw='{"service_name": "x"}',
                        arguments={"service_name": "x"},
                    )
                ],
            ),
            # Step 2: model corrects argument after seeing validation error
            LLMResponse(
                model="fake",
                prompt_tokens=140,
                completion_tokens=20,
                latency_ms=12.0,
                tool_calls=[
                    ToolCallRequest(
                        id="call_2",
                        name="lookup_service",
                        arguments_raw='{"service_name": "order-api"}',
                        arguments={"service_name": "order-api"},
                    )
                ],
            ),
            # Step 3: final answer
            LLMResponse(
                model="fake",
                content="order-api 当前状态健康。",
                prompt_tokens=180,
                completion_tokens=15,
                latency_ms=8.0,
            ),
        ]
    )

    engine = AgentEngine(fake, registry)
    result = await engine.run("检查 order-api", system_prompt="你是运维助手")

    assert result.stop_reason == StopReason.COMPLETED
    assert result.steps_taken == 3
    assert result.total_tokens == 475
    assert "健康" in result.final_answer
    assert not result.steps[0].tool_calls[0].ok
    assert result.steps[0].tool_calls[0].error_code == "INVALID_ARGUMENTS"
    assert result.steps[1].tool_calls[0].ok


async def test_agent_engine_repeated_tool_circuit_breaker() -> None:
    registry = _build_test_registry()
    repeated_call = ToolCallRequest(
        id="call_dup",
        name="lookup_service",
        arguments_raw='{"service_name": "order-api"}',
        arguments={"service_name": "order-api"},
    )
    fake = FakeLLMClient(
        [
            LLMResponse(model="fake", prompt_tokens=50, completion_tokens=10, tool_calls=[repeated_call]),
            LLMResponse(model="fake", prompt_tokens=60, completion_tokens=10, tool_calls=[repeated_call]),
        ]
    )

    engine = AgentEngine(fake, registry, config=AgentConfig(max_consecutive_duplicates=2))
    result = await engine.run("检查 order-api", system_prompt="你是运维助手")

    assert result.stop_reason == StopReason.REPEATED_TOOL_LOOP
    assert result.steps_taken == 2


async def test_agent_engine_max_steps_and_token_budget() -> None:
    registry = _build_test_registry()
    step1 = LLMResponse(
        model="fake",
        prompt_tokens=100,
        completion_tokens=50,
        tool_calls=[
            ToolCallRequest(
                id="c1",
                name="lookup_service",
                arguments_raw='{"service_name": "svc-a"}',
                arguments={"service_name": "svc-a"},
            )
        ],
    )
    step2 = LLMResponse(
        model="fake",
        prompt_tokens=100,
        completion_tokens=50,
        tool_calls=[
            ToolCallRequest(
                id="c2",
                name="lookup_service",
                arguments_raw='{"service_name": "svc-b"}',
                arguments={"service_name": "svc-b"},
            )
        ],
    )

    # Test token budget ceiling
    engine_budget = AgentEngine(
        FakeLLMClient([step1, step2]),
        registry,
        config=AgentConfig(max_steps=5, max_total_tokens=150),
    )
    res_budget = await engine_budget.run("检查", system_prompt="sys")
    assert res_budget.stop_reason == StopReason.TOKEN_BUDGET_EXCEEDED
    assert res_budget.steps_taken == 1

    # Test max steps ceiling
    engine_steps = AgentEngine(
        FakeLLMClient([step1, step2]),
        registry,
        config=AgentConfig(max_steps=2, max_total_tokens=10000),
    )
    res_steps = await engine_steps.run("检查", system_prompt="sys")
    assert res_steps.stop_reason == StopReason.MAX_STEPS
    assert res_steps.steps_taken == 2


def test_prune_messages_preserves_tool_groups() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "old huge result " + ("z" * 200)},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "b", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t2", "content": "recent result"},
    ]

    pruned, did_prune = prune_messages(messages, max_context_tokens=260)
    assert did_prune
    assert pruned[0]["role"] == "system"
    assert pruned[1] == {"role": "user", "content": "goal"}
    # Ensure no orphaned tool message exists without its preceding assistant(tool_calls)
    for idx, msg in enumerate(pruned):
        if msg["role"] == "tool":
            assert idx > 0
            assert pruned[idx - 1]["role"] == "assistant"
            assert pruned[idx - 1].get("tool_calls")
