"""Pydantic API request and response contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from enterprise_ops_agent.observability.tracer import TraceEvent, TraceSummary
from enterprise_ops_agent.runtime.types import PendingApproval


class DemoTokenRequest(BaseModel):
    """Request payload for issuing a signed demo Bearer token."""

    user_id: str = Field(default="alice", min_length=1)
    role: Literal["employee", "ops_admin", "sec_admin"] = "employee"
    department: str = Field(default="engineering", min_length=1)


class DemoTokenResponse(BaseModel):
    """Signed Bearer token response for interactive console and API clients."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user_id: str
    role: Literal["employee", "ops_admin", "sec_admin"]
    department: str


class ChatRequest(BaseModel):
    """Incoming chat or operational troubleshooting request."""

    # Fallback fields are ONLY used when AUTH_MODE=demo_rbac without a Bearer token.
    user_id: str = Field(default="alice", min_length=1)
    role: Literal["employee", "ops_admin", "sec_admin"] = "employee"
    department: str = Field(default="engineering", min_length=1)
    query: str = Field(min_length=1)
    use_router: bool = True
    model: str | None = None


class ChatResponse(BaseModel):
    """Normalized JSON response returned by /api/chat and /api/approvals/{id}."""

    run_id: str
    trace_id: str | None = None
    routed_model: str
    stop_reason: str
    final_answer: str
    steps_taken: int
    total_tokens: int
    total_latency_ms: float
    checkpoint_id: str | None = None
    pending_approval: PendingApproval | None = None
    security_events: list[str] = Field(default_factory=list)


class ApprovalDecisionRequest(BaseModel):
    """Human reviewer decision on a paused high-risk checkpoint."""

    approved: bool
    reviewer: str = Field(default="sre_lead", min_length=1)
    comment: str = ""


class PendingApprovalSummary(BaseModel):
    """Pending HITL checkpoint item displayed in the approval queue."""

    checkpoint_id: str
    run_id: str
    trace_id: str | None = None
    requested_by_user: str | None = None
    requested_by_role: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class TraceDetailResponse(BaseModel):
    """Full audit trace summary and chronological events."""

    summary: TraceSummary
    events: list[TraceEvent]
