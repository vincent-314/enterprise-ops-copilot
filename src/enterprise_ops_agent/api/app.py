"""FastAPI entrypoint exposing chat, SSE streaming, auth, HITL approvals, and audit replay."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from enterprise_ops_agent.api.auth import (
    can_review_high_risk,
    extract_bearer_token,
    issue_access_token,
    verify_access_token,
)
from enterprise_ops_agent.api.schemas import (
    ApprovalDecisionRequest,
    ChatRequest,
    ChatResponse,
    DemoTokenRequest,
    DemoTokenResponse,
    PendingApprovalSummary,
    TraceDetailResponse,
)
from enterprise_ops_agent.api.service import OpsCopilotService
from enterprise_ops_agent.tools.registry import UserContext

_STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


def _resolve_caller(
    service: OpsCopilotService,
    authorization: str | None,
    fallback_body: ChatRequest | None = None,
) -> UserContext:
    """Resolve verified caller identity; never trusts self-asserted role in token mode."""
    try:
        bearer = extract_bearer_token(authorization)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if bearer is not None:
        try:
            return verify_access_token(bearer, secret=service.settings.auth_secret)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    if service.settings.auth_mode == "demo_rbac" and fallback_body is not None:
        return UserContext(
            user_id=fallback_body.user_id,
            role=fallback_body.role,
            department=fallback_body.department,
        )

    raise HTTPException(
        status_code=401,
        detail="Missing Bearer token. Obtain one from POST /api/auth/demo-token.",
    )


def create_app(service: OpsCopilotService | None = None) -> FastAPI:
    """Create and configure the FastAPI application (supports test injection)."""
    shared_service = service or OpsCopilotService()
    manage_lifecycle = service is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await shared_service.startup()
        try:
            yield
        finally:
            if manage_lifecycle:
                await shared_service.shutdown()

    fastapi_app = FastAPI(
        title="Enterprise Ops Copilot API",
        version="0.2.0",
        lifespan=lifespan,
    )
    fastapi_app.state.service = shared_service

    @fastapi_app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not _STATIC_INDEX.exists():
            return HTMLResponse("<h1>Enterprise Ops Copilot</h1>")
        return HTMLResponse(_STATIC_INDEX.read_text(encoding="utf-8"))

    @fastapi_app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "locale": shared_service.settings.app_locale,
            "storage_backend": shared_service.settings.storage_backend,
            "auth_mode": shared_service.settings.auth_mode,
            "fast_model": shared_service.settings.router_fast_model,
            "strong_model": shared_service.settings.router_strong_model,
            "tools": shared_service.registry.list_names(),
        }

    @fastapi_app.post("/api/auth/demo-token", response_model=DemoTokenResponse)
    async def create_demo_token(req: DemoTokenRequest) -> DemoTokenResponse:
        if not shared_service.settings.allow_demo_tokens:
            raise HTTPException(
                status_code=403,
                detail="Demo token issuance is disabled in hardened production mode.",
            )
        user_ctx = UserContext(
            user_id=req.user_id,
            role=req.role,
            department=req.department,
        )
        token = issue_access_token(user_ctx, secret=shared_service.settings.auth_secret)
        return DemoTokenResponse(
            access_token=token,
            user_id=user_ctx.user_id,
            role=req.role,
            department=user_ctx.department,
        )

    @fastapi_app.post("/api/chat", response_model=ChatResponse)
    async def chat(
        req: ChatRequest,
        authorization: str | None = Header(default=None),
    ) -> ChatResponse:
        caller = _resolve_caller(shared_service, authorization, fallback_body=req)
        return await shared_service.chat(req, caller=caller)

    @fastapi_app.post("/api/chat/stream")
    async def chat_stream(
        req: ChatRequest,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        caller = _resolve_caller(shared_service, authorization, fallback_body=req)
        return StreamingResponse(
            shared_service.stream_chat(req, caller=caller),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @fastapi_app.get("/api/approvals/pending", response_model=list[PendingApprovalSummary])
    async def list_pending_approvals(
        authorization: str | None = Header(default=None),
    ) -> list[PendingApprovalSummary]:
        if shared_service.settings.auth_mode == "demo_rbac" and authorization is None:
            return shared_service.list_pending_approvals()
        caller = _resolve_caller(shared_service, authorization)
        items = shared_service.list_pending_approvals()
        if can_review_high_risk(caller.role):
            return items
        return [item for item in items if item.requested_by_user == caller.user_id]

    @fastapi_app.post("/api/approvals/{checkpoint_id}", response_model=ChatResponse)
    async def resolve_approval(
        checkpoint_id: str,
        decision: ApprovalDecisionRequest,
        authorization: str | None = Header(default=None),
    ) -> ChatResponse:
        if shared_service.settings.auth_mode == "demo_rbac" and authorization is None:
            reviewer = decision.reviewer
        else:
            caller = _resolve_caller(shared_service, authorization)
            if not can_review_high_risk(caller.role):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Role '{caller.role}' is not authorized to approve or reject "
                        "high-risk checkpoints; ops_admin or sec_admin required."
                    ),
                )
            reviewer = f"{caller.user_id} ({caller.role})"

        enforced_decision = ApprovalDecisionRequest(
            approved=decision.approved,
            reviewer=reviewer,
            comment=decision.comment,
        )
        try:
            return await shared_service.resolve_approval(checkpoint_id, enforced_decision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @fastapi_app.get("/api/traces/{trace_id}", response_model=TraceDetailResponse)
    async def get_trace(
        trace_id: str,
        authorization: str | None = Header(default=None),
    ) -> TraceDetailResponse:
        caller: UserContext | None = None
        if shared_service.settings.auth_mode != "demo_rbac" or authorization is not None:
            caller = _resolve_caller(shared_service, authorization)

        detail = shared_service.get_trace(trace_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Trace '{trace_id}' not found.")
        if (
            caller is not None
            and not can_review_high_risk(caller.role)
            and detail.summary.user_id != caller.user_id
        ):
            raise HTTPException(
                status_code=403,
                detail="Standard employees can only inspect their own execution traces.",
            )
        return detail

    @fastapi_app.get("/api/ops/status")
    async def ops_status() -> dict[str, Any]:
        return shared_service.get_ops_status()

    return fastapi_app


app = create_app()
