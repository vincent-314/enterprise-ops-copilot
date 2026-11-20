"""Query complexity router balancing latency (8B) and planning quality (14B)."""

from __future__ import annotations

from typing import Literal, Protocol

from openai import OpenAIError
from pydantic import BaseModel, Field, ValidationError

from enterprise_ops_agent.llm.client import LLMResponse
from enterprise_ops_agent.tools.registry import UserContext


class StructuredChatLLM(Protocol):
    """Protocol satisfied by LLMClient for structured routing calls."""

    async def chat_structured(
        self,
        messages: list[dict[str, object]],
        schema: type[BaseModel],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> tuple[BaseModel, LLMResponse]: ...


class TriageClassification(BaseModel):
    """Structured complexity classification produced by the fast router model."""

    tier: Literal["simple", "complex"]
    reason: str = Field(min_length=2, max_length=120)


class RouteDecision(BaseModel):
    """Final routing outcome including latency and selected model."""

    tier: Literal["simple", "complex"]
    target_model: str
    reason: str
    router_latency_ms: float = 0.0
    router_tokens: int = 0


_COMPLEX_FAST_PATH_KEYWORDS = (
    "回滚",
    "rollback",
    "重启",
    "restart",
    "根因",
    "p1",
    "应急",
    "哨兵",
    "跳板机",
    "故障转移",
)


class ComplexityRouter:
    """Routes simple FAQ/lookup tasks to a fast model and complex ops tasks to a stronger model."""

    def __init__(
        self,
        llm_client: StructuredChatLLM,
        *,
        fast_model: str = "qwen3:8b",
        strong_model: str = "qwen3:14b",
    ) -> None:
        self.llm = llm_client
        self.fast_model = fast_model
        self.strong_model = strong_model

    async def route(
        self,
        user_input: str,
        *,
        user_context: UserContext | None = None,
    ) -> RouteDecision:
        """Classify query complexity via fast rules + lightweight structured LLM triage."""
        lower = user_input.lower()
        for kw in _COMPLEX_FAST_PATH_KEYWORDS:
            if kw in lower:
                return RouteDecision(
                    tier="complex",
                    target_model=self.strong_model,
                    reason=f"fast-path keyword '{kw}'",
                )

        role_hint = user_context.role if user_context else "anonymous"
        messages: list[dict[str, object]] = [
            {
                "role": "system",
                "content": (
                    "你是企业智能路由分类器。判断用户请求属于 simple 还是 complex：\n"
                    "- simple：单次知识库问答（VPN/入职/通用说明）、单纯查看工单列表、或简单新建一个普通工单。\n"
                    "- complex：需要跨多系统串联排查指标与发布记录、分析故障根因、涉及高危变更或权限边界判断。"
                ),
            },
            {
                "role": "user",
                "content": f"caller_role={role_hint}\nquery={user_input}",
            },
        ]
        try:
            classified, resp = await self.llm.chat_structured(
                messages,
                TriageClassification,
                model=self.fast_model,
                temperature=0.0,
            )
            triage = TriageClassification.model_validate(classified)
            target = self.fast_model if triage.tier == "simple" else self.strong_model
            return RouteDecision(
                tier=triage.tier,
                target_model=target,
                reason=triage.reason,
                router_latency_ms=resp.latency_ms,
                router_tokens=resp.total_tokens,
            )
        except (OpenAIError, ValidationError, ValueError) as exc:
            # Fail-safe: route uncertain queries to the stronger model.
            return RouteDecision(
                tier="complex",
                target_model=self.strong_model,
                reason=f"router fallback due to {type(exc).__name__}",
            )
