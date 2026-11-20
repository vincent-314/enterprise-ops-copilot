"""Native async ReAct / Tool-Use agent runtime with HITL, guardrails, and audit tracing."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from enterprise_ops_agent.llm.client import LLMResponse, ToolCallRequest
from enterprise_ops_agent.observability.tracer import AuditTracer
from enterprise_ops_agent.runtime.checkpoints import (
    CheckpointStore,
    ExecutionCheckpoint,
)
from enterprise_ops_agent.runtime.context import prune_messages
from enterprise_ops_agent.runtime.types import (
    AgentRunResult,
    PendingApproval,
    StepRecord,
    StopReason,
    ToolCallRecord,
)
from enterprise_ops_agent.security.guardrails import GuardrailPolicy
from enterprise_ops_agent.tools.registry import RiskLevel, ToolRegistry, UserContext

_EVENT_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "agent_event_sink",
    default=None,
)


class ChatLLM(Protocol):
    """Minimal protocol satisfied by LLMClient and offline test fakes."""

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
    ) -> LLMResponse: ...


@dataclass(frozen=True)
class AgentConfig:
    """Safety and resource boundaries for a single agent run."""

    max_steps: int = 8
    max_total_tokens: int = 24000
    max_context_tokens: int = 12000
    max_consecutive_duplicates: int = 2


def _call_fingerprint(tc: ToolCallRequest) -> str:
    if tc.parse_error:
        return f"{tc.name}::PARSE_ERROR::{tc.arguments_raw}"
    canonical = json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)
    return f"{tc.name}::{canonical}"


def _assistant_message_from_response(resp: LLMResponse) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": resp.content or "",
    }
    if resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments_raw,
                },
            }
            for tc in resp.tool_calls
        ]
    return msg


class AgentEngine:
    """Explicit tool-use runtime with HITL gating, security guardrails, and audit tracing."""

    def __init__(
        self,
        llm_client: ChatLLM,
        tool_registry: ToolRegistry,
        config: AgentConfig | None = None,
        checkpoint_store: CheckpointStore | None = None,
        guardrails: GuardrailPolicy | None = None,
        tracer: AuditTracer | None = None,
    ) -> None:
        self.llm = llm_client
        self.registry = tool_registry
        self.config = config or AgentConfig()
        self.checkpoint_store = checkpoint_store
        self.guardrails = guardrails
        self.tracer = tracer

    def _log_event(
        self,
        *,
        trace_id: str,
        run_id: str,
        step_index: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if self.tracer is not None:
            self.tracer.record_event(
                trace_id=trace_id,
                run_id=run_id,
                step_index=step_index,
                event_type=event_type,
                payload=payload,
            )
        sink = _EVENT_SINK.get()
        if sink is not None:
            sink(
                {
                    "event": event_type,
                    "trace_id": trace_id,
                    "run_id": run_id,
                    "step": step_index,
                    "payload": payload,
                }
            )

    def _finalize_result(
        self,
        *,
        trace_id: str,
        run_id: str,
        final_answer: str,
        stop_reason: StopReason,
        steps_taken: int,
        total_prompt: int,
        total_completion: int,
        total_latency: float,
        steps: list[StepRecord],
        messages: list[dict[str, Any]],
        user_context: UserContext | None,
        security_events: list[str],
        checkpoint_id: str | None = None,
        pending_approval: PendingApproval | None = None,
    ) -> AgentRunResult:
        safe_answer = final_answer
        if self.guardrails is not None and stop_reason != StopReason.BLOCKED_BY_GUARDRAIL:
            dlp = self.guardrails.redact_output(safe_answer, user_context)
            if dlp.redacted:
                safe_answer = dlp.text
                event_tag = f"DLP_REDACTED:{','.join(dlp.matched_patterns)}"
                security_events.append(event_tag)
                self._log_event(
                    trace_id=trace_id,
                    run_id=run_id,
                    step_index=steps_taken,
                    event_type="guardrail_dlp_redacted",
                    payload={"matched_patterns": dlp.matched_patterns},
                )
                if messages and messages[-1].get("role") == "assistant":
                    messages[-1]["content"] = safe_answer

        total_tokens = total_prompt + total_completion
        if self.tracer is not None:
            trace_status = (
                "paused" if stop_reason == StopReason.AWAITING_APPROVAL else "finished"
            )
            event_name = (
                "hitl_paused" if stop_reason == StopReason.AWAITING_APPROVAL else "run_finished"
            )
            self._log_event(
                trace_id=trace_id,
                run_id=run_id,
                step_index=steps_taken,
                event_type=event_name,
                payload={
                    "stop_reason": stop_reason.value,
                    "checkpoint_id": checkpoint_id,
                    "total_tokens": total_tokens,
                    "final_answer_preview": safe_answer[:240],
                },
            )
            self.tracer.finish_trace(
                trace_id=trace_id,
                status=trace_status,
                stop_reason=stop_reason.value,
                total_tokens=total_tokens,
                total_latency_ms=total_latency,
                security_events=security_events,
            )

        return AgentRunResult(
            run_id=run_id,
            trace_id=trace_id,
            final_answer=safe_answer,
            stop_reason=stop_reason,
            steps_taken=steps_taken,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_tokens=total_tokens,
            total_latency_ms=round(total_latency, 1),
            steps=steps,
            messages=messages,
            checkpoint_id=checkpoint_id,
            pending_approval=pending_approval,
            security_events=list(security_events),
        )

    async def run(
        self,
        user_input: str,
        *,
        system_prompt: str,
        history: list[dict[str, Any]] | None = None,
        model: str | None = None,
        user_context: UserContext | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentRunResult:
        """Execute the agent loop until completion, approval pause, or safety boundary."""
        token = _EVENT_SINK.set(on_event)
        try:
            return await self._run_impl(
                user_input=user_input,
                system_prompt=system_prompt,
                history=history,
                model=model,
                user_context=user_context,
            )
        finally:
            _EVENT_SINK.reset(token)

    async def _run_impl(
        self,
        *,
        user_input: str,
        system_prompt: str,
        history: list[dict[str, Any]] | None = None,
        model: str | None = None,
        user_context: UserContext | None = None,
    ) -> AgentRunResult:
        run_id = uuid.uuid4().hex[:12]
        trace_id = f"trc_{uuid.uuid4().hex[:12]}"
        security_events: list[str] = []

        if self.tracer is not None:
            self.tracer.start_trace(
                trace_id=trace_id,
                run_id=run_id,
                user_id=user_context.user_id if user_context else None,
                user_role=user_context.role if user_context else None,
                model=model,
                user_input=user_input,
            )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_input})

        if self.guardrails is not None:
            decision = self.guardrails.check_user_input(user_input)
            if not decision.allowed:
                security_events.append(f"INPUT_BLOCKED:{decision.matched_rule}")
                self._log_event(
                    trace_id=trace_id,
                    run_id=run_id,
                    step_index=0,
                    event_type="guardrail_input_blocked",
                    payload={"matched_rule": decision.matched_rule, "reason": decision.reason},
                )
                return self._finalize_result(
                    trace_id=trace_id,
                    run_id=run_id,
                    final_answer=decision.reason or "请求被安全护栏拦截。",
                    stop_reason=StopReason.BLOCKED_BY_GUARDRAIL,
                    steps_taken=0,
                    total_prompt=0,
                    total_completion=0,
                    total_latency=0.0,
                    steps=[],
                    messages=messages,
                    user_context=user_context,
                    security_events=security_events,
                )

        return await self._run_loop(
            trace_id=trace_id,
            run_id=run_id,
            messages=messages,
            model=model,
            user_context=user_context,
            start_step=1,
            steps=[],
            total_prompt=0,
            total_completion=0,
            total_latency=0.0,
            security_events=security_events,
        )

    async def resume(
        self,
        checkpoint_id: str,
        *,
        approved: bool,
        reviewer: str,
        comment: str = "",
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentRunResult:
        """Resume a paused run after a human approves or rejects the gated high-risk call."""
        token = _EVENT_SINK.set(on_event)
        try:
            return await self._resume_impl(
                checkpoint_id=checkpoint_id,
                approved=approved,
                reviewer=reviewer,
                comment=comment,
            )
        finally:
            _EVENT_SINK.reset(token)

    async def _resume_impl(
        self,
        *,
        checkpoint_id: str,
        approved: bool,
        reviewer: str,
        comment: str = "",
    ) -> AgentRunResult:
        if self.checkpoint_store is None:
            raise RuntimeError("Cannot resume without a configured CheckpointStore.")

        ckpt = self.checkpoint_store.get(checkpoint_id)
        if ckpt is None:
            raise KeyError(f"Checkpoint '{checkpoint_id}' does not exist.")
        if ckpt.status != "pending":
            raise ValueError(
                f"Checkpoint '{checkpoint_id}' already resolved with status='{ckpt.status}'."
            )
        if not ckpt.pending_tool_calls:
            raise ValueError(f"Checkpoint '{checkpoint_id}' has no pending tool calls.")

        trace_id = ckpt.trace_id or f"trc_{uuid.uuid4().hex[:12]}"
        security_events = list(ckpt.security_events)
        messages = list(ckpt.messages)
        steps = list(ckpt.steps)
        gated_call = ckpt.pending_tool_calls[0]
        remaining_calls = ckpt.pending_tool_calls[1:]

        self._log_event(
            trace_id=trace_id,
            run_id=ckpt.run_id,
            step_index=ckpt.steps_taken_so_far,
            event_type="hitl_resumed",
            payload={
                "checkpoint_id": checkpoint_id,
                "approved": approved,
                "reviewer": reviewer,
                "comment": comment,
                "tool_name": gated_call.name,
            },
        )

        if not approved:
            self.checkpoint_store.update_decision(
                checkpoint_id,
                status="rejected",
                reviewer=reviewer,
                comment=comment,
            )
            rejection_payload = json.dumps(
                {
                    "ok": False,
                    "error": "APPROVAL_REJECTED",
                    "reviewer": reviewer,
                    "reason": comment or "管理员驳回了该高危操作请求。",
                },
                ensure_ascii=False,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": gated_call.id,
                    "content": rejection_payload,
                }
            )
            call_records = [
                ToolCallRecord(
                    tool_name=gated_call.name,
                    arguments=gated_call.arguments,
                    ok=False,
                    error_code="APPROVAL_REJECTED",
                    output_preview=rejection_payload[:120],
                )
            ]
            for trailing in remaining_calls:
                skip_msg = json.dumps(
                    {
                        "ok": False,
                        "error": "SKIPPED_DUE_TO_REJECTION",
                        "message": f"Skipped because '{gated_call.name}' was rejected by {reviewer}.",
                    },
                    ensure_ascii=False,
                )
                messages.append(
                    {"role": "tool", "tool_call_id": trailing.id, "content": skip_msg}
                )
                call_records.append(
                    ToolCallRecord(
                        tool_name=trailing.name,
                        arguments=trailing.arguments,
                        ok=False,
                        error_code="SKIPPED_DUE_TO_REJECTION",
                        output_preview=skip_msg[:120],
                    )
                )
            steps.append(
                StepRecord(
                    step_index=ckpt.steps_taken_so_far,
                    latency_ms=0.0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    tool_calls=call_records,
                    assistant_preview=f"[HITL Rejected by {reviewer}]",
                )
            )
        else:
            self.checkpoint_store.update_decision(
                checkpoint_id,
                status="approved",
                reviewer=reviewer,
                comment=comment,
            )
            paused, call_records = await self._execute_tool_batch(
                trace_id=trace_id,
                tool_calls=ckpt.pending_tool_calls,
                messages=messages,
                approved_call_ids={gated_call.id},
                run_id=ckpt.run_id,
                model=ckpt.model,
                user_context=ckpt.user_context,
                step_idx=ckpt.steps_taken_so_far,
                total_prompt=ckpt.total_prompt_tokens,
                total_completion=ckpt.total_completion_tokens,
                total_latency=ckpt.total_latency_ms,
                steps=steps,
                security_events=security_events,
            )
            if paused is not None:
                return paused

            steps.append(
                StepRecord(
                    step_index=ckpt.steps_taken_so_far,
                    latency_ms=0.0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    tool_calls=call_records,
                    assistant_preview=f"[HITL Approved by {reviewer}]",
                )
            )

        return await self._run_loop(
            trace_id=trace_id,
            run_id=ckpt.run_id,
            messages=messages,
            model=ckpt.model,
            user_context=ckpt.user_context,
            start_step=ckpt.steps_taken_so_far + 1,
            steps=steps,
            total_prompt=ckpt.total_prompt_tokens,
            total_completion=ckpt.total_completion_tokens,
            total_latency=ckpt.total_latency_ms,
            security_events=security_events,
        )

    async def _execute_tool_batch(
        self,
        *,
        trace_id: str,
        tool_calls: list[ToolCallRequest],
        messages: list[dict[str, Any]],
        approved_call_ids: set[str],
        run_id: str,
        model: str | None,
        user_context: UserContext | None,
        step_idx: int,
        total_prompt: int,
        total_completion: int,
        total_latency: float,
        steps: list[StepRecord],
        security_events: list[str],
    ) -> tuple[AgentRunResult | None, list[ToolCallRecord]]:
        call_records: list[ToolCallRecord] = []

        for idx, tc in enumerate(tool_calls):
            if tc.parse_error is not None:
                err_payload = json.dumps(
                    {
                        "ok": False,
                        "error": "MALFORMED_TOOL_ARGUMENTS",
                        "message": tc.parse_error,
                    },
                    ensure_ascii=False,
                )
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_payload})
                call_records.append(
                    ToolCallRecord(
                        tool_name=tc.name,
                        arguments={},
                        ok=False,
                        error_code="MALFORMED_TOOL_ARGUMENTS",
                        output_preview=err_payload[:120],
                    )
                )
                continue

            spec = self.registry.get(tc.name)

            # Layer 2 deterministic RBAC check BEFORE HITL or execution.
            if self.guardrails is not None and spec is not None:
                auth_decision = self.guardrails.check_tool_authorization(
                    tc.name,
                    spec.risk_level,
                    user_context,
                )
                if not auth_decision.allowed:
                    security_events.append(f"ROLE_DENIED:{tc.name}")
                    self._log_event(
                        trace_id=trace_id,
                        run_id=run_id,
                        step_index=step_idx,
                        event_type="policy_denied",
                        payload={
                            "tool_name": tc.name,
                            "risk_level": spec.risk_level.value,
                            "user_role": user_context.role if user_context else None,
                            "matched_rule": auth_decision.matched_rule,
                        },
                    )
                    denied_payload = json.dumps(
                        {
                            "ok": False,
                            "error": "ROLE_NOT_AUTHORIZED",
                            "message": auth_decision.reason,
                        },
                        ensure_ascii=False,
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": denied_payload}
                    )
                    call_records.append(
                        ToolCallRecord(
                            tool_name=tc.name,
                            arguments=tc.arguments,
                            ok=False,
                            error_code="ROLE_NOT_AUTHORIZED",
                            output_preview=denied_payload[:120],
                        )
                    )
                    continue

            if (
                spec is not None
                and spec.risk_level == RiskLevel.HIGH_RISK
                and tc.id not in approved_call_ids
            ):
                ckpt_id = f"ckpt_{uuid.uuid4().hex[:10]}"
                if self.checkpoint_store is not None:
                    self.checkpoint_store.save(
                        ExecutionCheckpoint(
                            checkpoint_id=ckpt_id,
                            run_id=run_id,
                            trace_id=trace_id,
                            status="pending",
                            model=model,
                            user_context=user_context,
                            messages=list(messages),
                            pending_tool_calls=list(tool_calls[idx:]),
                            steps_taken_so_far=step_idx,
                            total_prompt_tokens=total_prompt,
                            total_completion_tokens=total_completion,
                            total_latency_ms=round(total_latency, 1),
                            steps=list(steps),
                            security_events=list(security_events),
                        )
                    )
                pending = PendingApproval(
                    checkpoint_id=ckpt_id if self.checkpoint_store is not None else None,
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    risk_level=spec.risk_level.value,
                    requested_by_user=user_context.user_id if user_context else None,
                )
                paused_result = self._finalize_result(
                    trace_id=trace_id,
                    run_id=run_id,
                    final_answer=(
                        f"操作已挂起等待人工审批：高危工具 `{tc.name}` "
                        f"参数={json.dumps(tc.arguments, ensure_ascii=False)}，"
                        f"审批单号={pending.checkpoint_id or '(ephemeral)'}。"
                    ),
                    stop_reason=StopReason.AWAITING_APPROVAL,
                    steps_taken=step_idx,
                    total_prompt=total_prompt,
                    total_completion=total_completion,
                    total_latency=total_latency,
                    steps=steps,
                    messages=messages,
                    user_context=user_context,
                    security_events=security_events,
                    checkpoint_id=pending.checkpoint_id,
                    pending_approval=pending,
                )
                return paused_result, call_records

            exec_result = await self.registry.execute(
                tc.name,
                tc.arguments,
                user_context=user_context,
            )
            tool_content = exec_result.output
            injection_flagged = False
            if self.guardrails is not None and exec_result.ok:
                sanitized = self.guardrails.sanitize_tool_output(tc.name, exec_result.output)
                tool_content = sanitized.content
                injection_flagged = sanitized.injection_detected
                if injection_flagged:
                    security_events.append(
                        f"INDIRECT_INJECTION:{tc.name}:{','.join(sanitized.matched_patterns)}"
                    )
                    self._log_event(
                        trace_id=trace_id,
                        run_id=run_id,
                        step_index=step_idx,
                        event_type="guardrail_injection_flagged",
                        payload={
                            "tool_name": tc.name,
                            "matched_patterns": sanitized.matched_patterns,
                        },
                    )

            self._log_event(
                trace_id=trace_id,
                run_id=run_id,
                step_index=step_idx,
                event_type="tool_executed",
                payload={
                    "tool_name": tc.name,
                    "arguments": tc.arguments,
                    "ok": exec_result.ok,
                    "error_code": exec_result.error_code,
                    "injection_flagged": injection_flagged,
                    "output_preview": exec_result.output[:180],
                },
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_content})
            call_records.append(
                ToolCallRecord(
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    ok=exec_result.ok,
                    error_code=exec_result.error_code,
                    output_preview=exec_result.output[:120],
                )
            )

        return None, call_records

    async def _run_loop(
        self,
        *,
        trace_id: str,
        run_id: str,
        messages: list[dict[str, Any]],
        model: str | None,
        user_context: UserContext | None,
        start_step: int,
        steps: list[StepRecord],
        total_prompt: int,
        total_completion: int,
        total_latency: float,
        security_events: list[str],
    ) -> AgentRunResult:
        openai_tools = self.registry.to_openai_tools()
        last_batch_fingerprint: tuple[str, ...] | None = None
        consecutive_dup_count = 0

        for step_idx in range(start_step, self.config.max_steps + 1):
            bounded_messages, pruned = prune_messages(messages, self.config.max_context_tokens)
            messages = bounded_messages

            resp = await self.llm.chat(
                messages,
                tools=openai_tools or None,
                model=model,
            )
            total_prompt += resp.prompt_tokens
            total_completion += resp.completion_tokens
            total_latency += resp.latency_ms

            self._log_event(
                trace_id=trace_id,
                run_id=run_id,
                step_index=step_idx,
                event_type="llm_turn",
                payload={
                    "latency_ms": resp.latency_ms,
                    "prompt_tokens": resp.prompt_tokens,
                    "completion_tokens": resp.completion_tokens,
                    "pruned_context": pruned,
                    "tool_calls": [tc.name for tc in resp.tool_calls],
                    "assistant_preview": resp.content[:140],
                },
            )

            if not resp.tool_calls:
                steps.append(
                    StepRecord(
                        step_index=step_idx,
                        latency_ms=resp.latency_ms,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        pruned_context=pruned,
                        assistant_preview=resp.content[:120],
                    )
                )
                messages.append({"role": "assistant", "content": resp.content})
                return self._finalize_result(
                    trace_id=trace_id,
                    run_id=run_id,
                    final_answer=resp.content,
                    stop_reason=StopReason.COMPLETED,
                    steps_taken=step_idx,
                    total_prompt=total_prompt,
                    total_completion=total_completion,
                    total_latency=total_latency,
                    steps=steps,
                    messages=messages,
                    user_context=user_context,
                    security_events=security_events,
                )

            batch_fp = tuple(_call_fingerprint(tc) for tc in resp.tool_calls)
            if batch_fp == last_batch_fingerprint:
                consecutive_dup_count += 1
            else:
                last_batch_fingerprint = batch_fp
                consecutive_dup_count = 1

            if consecutive_dup_count >= self.config.max_consecutive_duplicates:
                steps.append(
                    StepRecord(
                        step_index=step_idx,
                        latency_ms=resp.latency_ms,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        pruned_context=pruned,
                        assistant_preview="[Circuit broken: repeated identical tool call]",
                    )
                )
                return self._finalize_result(
                    trace_id=trace_id,
                    run_id=run_id,
                    final_answer=(
                        "任务已安全终止：检测到模型连续发起完全相同的工具调用，已触发防死循环熔断。"
                    ),
                    stop_reason=StopReason.REPEATED_TOOL_LOOP,
                    steps_taken=step_idx,
                    total_prompt=total_prompt,
                    total_completion=total_completion,
                    total_latency=total_latency,
                    steps=steps,
                    messages=messages,
                    user_context=user_context,
                    security_events=security_events,
                )

            messages.append(_assistant_message_from_response(resp))
            paused_result, call_records = await self._execute_tool_batch(
                trace_id=trace_id,
                tool_calls=resp.tool_calls,
                messages=messages,
                approved_call_ids=set(),
                run_id=run_id,
                model=model,
                user_context=user_context,
                step_idx=step_idx,
                total_prompt=total_prompt,
                total_completion=total_completion,
                total_latency=total_latency,
                steps=steps,
                security_events=security_events,
            )

            steps.append(
                StepRecord(
                    step_index=step_idx,
                    latency_ms=resp.latency_ms,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    pruned_context=pruned,
                    tool_calls=call_records,
                    assistant_preview=resp.content[:120],
                )
            )

            if paused_result is not None:
                if paused_result.checkpoint_id and self.checkpoint_store is not None:
                    ckpt = self.checkpoint_store.get(paused_result.checkpoint_id)
                    if ckpt is not None:
                        ckpt.steps = list(steps)
                        ckpt.security_events = list(security_events)
                        self.checkpoint_store.save(ckpt)
                return paused_result

            if (total_prompt + total_completion) >= self.config.max_total_tokens:
                return self._finalize_result(
                    trace_id=trace_id,
                    run_id=run_id,
                    final_answer="任务已安全终止：单次运行 Token 预算已达上限。",
                    stop_reason=StopReason.TOKEN_BUDGET_EXCEEDED,
                    steps_taken=step_idx,
                    total_prompt=total_prompt,
                    total_completion=total_completion,
                    total_latency=total_latency,
                    steps=steps,
                    messages=messages,
                    user_context=user_context,
                    security_events=security_events,
                )

        return self._finalize_result(
            trace_id=trace_id,
            run_id=run_id,
            final_answer=f"任务已安全终止：已达到最大推理步数上限（{self.config.max_steps} 步）。",
            stop_reason=StopReason.MAX_STEPS,
            steps_taken=self.config.max_steps,
            total_prompt=total_prompt,
            total_completion=total_completion,
            total_latency=total_latency,
            steps=steps,
            messages=messages,
            user_context=user_context,
            security_events=security_events,
        )
