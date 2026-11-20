"""Async MCP Client session bridge that registers discovered MCP tools into ToolRegistry."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Self

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent, ToolAnnotations

from enterprise_ops_agent.tools.registry import RiskLevel, ToolRegistry


def risk_from_annotations(
    annotations: ToolAnnotations | None,
    *,
    tool_name: str = "",
    overrides: dict[str, RiskLevel] | None = None,
) -> RiskLevel:
    """Map MCP ToolAnnotations to internal RiskLevel with optional local policy overrides."""
    if overrides and tool_name in overrides:
        return overrides[tool_name]
    if annotations is None:
        return RiskLevel.WRITE_LOW
    destructive = getattr(annotations, "destructive_hint", None)
    if destructive is None:
        destructive = getattr(annotations, "destructiveHint", False)
    if destructive:
        return RiskLevel.HIGH_RISK
    read_only = getattr(annotations, "read_only_hint", None)
    if read_only is None:
        read_only = getattr(annotations, "readOnlyHint", False)
    if read_only:
        return RiskLevel.READ
    return RiskLevel.WRITE_LOW


def _extract_call_result_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif hasattr(item, "text"):
            parts.append(str(item.text))
        else:
            parts.append(str(item))
    text = "\n".join(parts).strip()
    if getattr(result, "isError", False) and not text.startswith("{"):
        return json.dumps(
            {"ok": False, "error": "MCP_TOOL_ERROR", "message": text},
            ensure_ascii=False,
        )
    return text or json.dumps({"ok": not getattr(result, "isError", False)}, ensure_ascii=False)


class MCPToolSession:
    """Manages a stdio MCP server subprocess in an isolated task to keep anyio scopes intact."""

    def __init__(
        self,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        risk_overrides: dict[str, RiskLevel] | None = None,
    ) -> None:
        self.command = command or sys.executable
        self.args = args or ["-m", "enterprise_ops_agent.mcp_bridge.ops_server"]
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        self.env = merged_env
        self.risk_overrides = risk_overrides or {}
        self.session: ClientSession | None = None
        self._ready_event: asyncio.Event | None = None
        self._stop_event: asyncio.Event | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._startup_error: BaseException | None = None

    async def _run_session_lifecycle(self) -> None:
        assert self._ready_event is not None
        assert self._stop_event is not None
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
        )
        try:
            async with (
                stdio_client(params) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                self.session = session
                self._ready_event.set()
                await self._stop_event.wait()
        except BaseException as exc:  # noqa: BLE001 — propagate startup failure to __aenter__
            self._startup_error = exc
            self._ready_event.set()
        finally:
            self.session = None

    async def __aenter__(self) -> Self:
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._startup_error = None
        self._runner_task = asyncio.create_task(self._run_session_lifecycle())
        await self._ready_event.wait()
        if self._startup_error is not None:
            await self.__aexit__(None, None, None)
            raise self._startup_error
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._runner_task is not None:
            await asyncio.gather(self._runner_task, return_exceptions=True)
            self._runner_task = None
        self.session = None

    async def register_tools(self, registry: ToolRegistry) -> list[str]:
        """Discover all tools exposed by the MCP server and register them into ToolRegistry."""
        if self.session is None:
            raise RuntimeError("MCPToolSession must be entered before registering tools.")

        listed = await self.session.list_tools()
        registered: list[str] = []
        for tool in listed.tools:
            risk = risk_from_annotations(
                tool.annotations,
                tool_name=tool.name,
                overrides=self.risk_overrides,
            )
            schema = getattr(tool, "input_schema", None)
            if schema is None:
                schema = getattr(tool, "inputSchema", {"type": "object", "properties": {}})

            async def _invoke(arguments: dict[str, Any], _name: str = tool.name) -> str:
                if self.session is None:
                    raise RuntimeError("MCP session is closed.")
                result = await self.session.call_tool(_name, arguments=arguments)
                return _extract_call_result_text(result)

            registry.register_external(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(schema),
                handler=_invoke,
                risk_level=risk,
            )
            registered.append(tool.name)
        return registered
