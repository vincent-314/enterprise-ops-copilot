"""Data models for agent execution traces, termination reasons, and HITL approvals."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class StopReason(StrEnum):
    """Explicit reason why the agent runtime loop terminated or paused."""

    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED_BY_GUARDRAIL = "blocked_by_guardrail"
    MAX_STEPS = "max_steps"
    REPEATED_TOOL_LOOP = "repeated_tool_loop"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"


class PendingApproval(BaseModel):
    """Human-in-the-loop approval request emitted when a high-risk tool is gated."""

    checkpoint_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    requested_by_user: str | None = None


class ToolCallRecord(BaseModel):
    """Observed single tool invocation inside a runtime step."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    error_code: str | None = None
    output_preview: str = ""


class StepRecord(BaseModel):
    """Per-iteration telemetry captured by the AgentEngine."""

    step_index: int
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    pruned_context: bool = False
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    assistant_preview: str = ""


class AgentRunResult(BaseModel):
    """Complete result and telemetry of an agent execution (or pause)."""

    run_id: str
    final_answer: str
    stop_reason: StopReason
    steps_taken: int
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    steps: list[StepRecord] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint_id: str | None = None
    pending_approval: PendingApproval | None = None
    trace_id: str | None = None
    security_events: list[str] = Field(default_factory=list)
