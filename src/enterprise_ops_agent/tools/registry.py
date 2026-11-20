"""Declarative Pydantic-first and MCP-compatible tool registry with identity injection."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

ToolHandler = Callable[..., str | Awaitable[str]]


class UserContext(BaseModel):
    """Immutable authenticated caller identity injected by the runtime (never the LLM)."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    role: str
    department: str = "general"


class RiskLevel(StrEnum):
    """Operational risk classification for enterprise tool governance."""

    READ = "read"
    WRITE_LOW = "write_low"
    HIGH_RISK = "high_risk"


class ToolExecutionResult(BaseModel):
    """Normalized outcome of executing a single tool call."""

    tool_name: str
    ok: bool
    output: str
    error_code: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """Registered tool metadata, schema source, and execution handler."""

    name: str
    description: str
    handler: ToolHandler
    args_model: type[BaseModel] | None = None
    input_schema: dict[str, Any] | None = None
    risk_level: RiskLevel = RiskLevel.READ
    requires_context: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate an OpenAI-compatible function tool definition."""
        if self.args_model is not None:
            raw_schema = self.args_model.model_json_schema()
        elif self.input_schema is not None:
            raw_schema = dict(self.input_schema)
        else:
            raw_schema = {"type": "object", "properties": {}}

        raw_schema.pop("title", None)
        raw_schema.pop("$schema", None)
        for prop in raw_schema.get("properties", {}).values():
            if isinstance(prop, dict):
                prop.pop("title", None)
        raw_schema.setdefault("additionalProperties", False)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": raw_schema,
            },
        }


def _error_payload(code: str, message: str, **extra: Any) -> str:
    payload = {"ok": False, "error": code, "message": message, **extra}
    return json.dumps(payload, ensure_ascii=False)


class ToolRegistry:
    """Unified registry for local Pydantic tools and remote MCP-discovered tools."""

    def __init__(self, max_output_chars: int = 6000) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.max_output_chars = max_output_chars

    def register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        handler: ToolHandler,
        risk_level: RiskLevel = RiskLevel.READ,
    ) -> None:
        """Register a local Pydantic-validated tool; 2-arg handlers receive UserContext."""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        param_count = len(inspect.signature(handler).parameters)
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            args_model=args_model,
            handler=handler,
            risk_level=risk_level,
            requires_context=param_count >= 2,
        )

    def register_external(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
        risk_level: RiskLevel = RiskLevel.READ,
        *,
        requires_context: bool = False,
    ) -> None:
        """Register an externally discovered tool (e.g. from an MCP Server)."""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            handler=handler,
            input_schema=input_schema,
            risk_level=risk_level,
            requires_context=requires_context,
        )

    def get(self, name: str) -> ToolSpec | None:
        """Look up a registered tool spec."""
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._tools.keys())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Serialize all registered tools for the Chat Completions API."""
        return [spec.to_openai_schema() for spec in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_context: UserContext | None = None,
    ) -> ToolExecutionResult:
        """Validate arguments, inject UserContext if needed, and normalize output."""
        spec = self._tools.get(name)
        if spec is None:
            available = sorted(self._tools.keys())
            return ToolExecutionResult(
                tool_name=name,
                ok=False,
                error_code="UNKNOWN_TOOL",
                output=_error_payload(
                    "UNKNOWN_TOOL",
                    f"Tool '{name}' does not exist.",
                    available_tools=available,
                ),
            )

        if spec.requires_context and user_context is None:
            return ToolExecutionResult(
                tool_name=name,
                ok=False,
                error_code="MISSING_USER_CONTEXT",
                output=_error_payload(
                    "MISSING_USER_CONTEXT",
                    f"Tool '{name}' requires an authenticated UserContext.",
                ),
            )

        parsed_arg: Any = arguments
        if spec.args_model is not None:
            try:
                parsed_arg = spec.args_model.model_validate(arguments)
            except ValidationError as exc:
                return ToolExecutionResult(
                    tool_name=name,
                    ok=False,
                    error_code="INVALID_ARGUMENTS",
                    output=_error_payload(
                        "INVALID_ARGUMENTS",
                        f"Argument validation failed for '{name}'. Fix arguments and retry.",
                        validation_errors=exc.errors(include_url=False),
                    ),
                )

        try:
            if spec.requires_context:
                result = spec.handler(parsed_arg, user_context)
            else:
                result = spec.handler(parsed_arg)
            raw_output = await result if inspect.isawaitable(result) else result
        except Exception as exc:  # noqa: BLE001 — tool boundary must convert crashes into feedback
            return ToolExecutionResult(
                tool_name=name,
                ok=False,
                error_code="TOOL_EXECUTION_ERROR",
                output=_error_payload(
                    "TOOL_EXECUTION_ERROR",
                    f"Tool '{name}' failed at runtime: {type(exc).__name__}: {exc}",
                ),
            )

        truncated = False
        output = str(raw_output)
        if len(output) > self.max_output_chars:
            truncated = True
            output = (
                output[: self.max_output_chars]
                + f"\n...[truncated {len(output) - self.max_output_chars} chars]"
            )

        return ToolExecutionResult(
            tool_name=name,
            ok=True,
            output=output,
            truncated=truncated,
        )
