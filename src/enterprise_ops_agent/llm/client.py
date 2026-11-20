"""Observable OpenAI-compatible LLM client wrapper.

Decouples the agent runtime from specific inference providers (Ollama, vLLM,
or remote APIs) while normalizing token usage, latency, and tool-call parsing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from enterprise_ops_agent.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


class ToolCallRequest(BaseModel):
    """Normalized representation of a model-requested tool call."""

    id: str
    name: str
    arguments_raw: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    parse_error: str | None = None


class LLMResponse(BaseModel):
    """Normalized LLM completion response with observability metrics."""

    model: str
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0


def _parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """Parse tool-call JSON arguments without crashing the runtime loop."""
    if not raw or not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON arguments: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"Tool arguments must be a JSON object, got {type(parsed).__name__}"
    return parsed, None


def _strip_code_fence(text: str) -> str:
    """Remove optional Markdown JSON code fences around structured responses."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = _CODE_FENCE_RE.sub("", cleaned).strip()
    return cleaned


class LLMClient:
    """Thin async gateway around any OpenAI-compatible chat completions API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = AsyncOpenAI(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            timeout=self.settings.llm_timeout_seconds,
        )

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
        """Send a chat completion request and return a normalized LLMResponse."""
        target_model = model or self.settings.llm_model
        kwargs: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": self.settings.llm_max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        # Note: Ollama's /v1/chat/completions endpoint ignores options.num_ctx;
        # configure OLLAMA_CONTEXT_LENGTH on the Ollama server process instead.
        if extra_body is not None:
            kwargs["extra_body"] = extra_body

        started = time.perf_counter()
        completion = await self._client.chat.completions.create(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000.0

        choice = completion.choices[0]
        message = choice.message
        usage = completion.usage

        tool_calls: list[ToolCallRequest] = []
        for tc in message.tool_calls or []:
            parsed_args, parse_error = _parse_tool_arguments(tc.function.arguments)
            tool_calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments_raw=tc.function.arguments,
                    arguments=parsed_args,
                    parse_error=parse_error,
                )
            )

        reasoning = getattr(message, "reasoning_content", None) or getattr(
            message, "reasoning", None
        )

        return LLMResponse(
            model=completion.model or target_model,
            content=message.content or "",
            reasoning_content=reasoning,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            latency_ms=round(latency_ms, 1),
        )

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> tuple[T, LLMResponse]:
        """Request JSON conforming to a Pydantic model and validate the output."""
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
                "strict": True,
            },
        }
        resp = await self.chat(
            messages=messages,
            response_format=response_format,
            model=model,
            temperature=temperature,
        )
        cleaned = _strip_code_fence(resp.content)
        parsed = schema.model_validate_json(cleaned)
        return parsed, resp

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate dense vector embeddings via the OpenAI-compatible /v1/embeddings API."""
        if not texts:
            return []
        target_model = model or self.settings.embedding_model
        response = await self._client.embeddings.create(
            model=target_model,
            input=texts,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]
