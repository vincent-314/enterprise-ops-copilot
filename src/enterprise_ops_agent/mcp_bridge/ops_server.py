"""Standard MCP Server exposing internal ticket, service health, and remediation tools."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from enterprise_ops_agent.ops_backend import OpsStore

mcp = MCPServer("enterprise-ops-mcp")


def _get_store() -> Any:
    backend = os.environ.get("STORAGE_BACKEND", "sqlite")
    locale_raw = os.environ.get("APP_LOCALE", "en")
    locale: Literal["en", "zh"] = "zh" if locale_raw == "zh" else "en"
    if backend == "postgres":
        from enterprise_ops_agent.storage.postgres import PostgresOpsStore

        dsn = os.environ.get(
            "POSTGRES_DSN",
            "postgresql://postgres:postgres@localhost:5432/ops_copilot",
        )
        return PostgresOpsStore(dsn, locale=locale)
    db_path = os.environ.get("OPS_DB_PATH", "data/ops.db")
    return OpsStore(db_path, locale=locale)


@mcp.tool(
    description="List internal IT/Ops incident tickets filtered by status and optional severity.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def list_incidents(
    status: Literal["open", "investigating", "closed", "all"] = "open",
    severity: Literal["P1", "P2", "P3", "all"] = "all",
) -> str:
    incidents = _get_store().list_incidents(status, severity=severity)
    return json.dumps(
        {"ok": True, "count": len(incidents), "incidents": incidents},
        ensure_ascii=False,
    )


@mcp.tool(
    description=(
        "Fetch live CPU, memory, latency, error rate, and log excerpt for an exact service_name. "
        "Call list_incidents first if you do not yet know the exact service_name; never guess placeholders."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def get_service_metrics(service_name: str) -> str:
    store = _get_store()
    data = store.get_service_metrics(service_name)
    if data is None:
        return json.dumps(
            {
                "ok": False,
                "error": "SERVICE_NOT_FOUND",
                "known_services": store.list_known_services(),
            },
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


@mcp.tool(
    description=(
        "Search recent release/deployment records for an exact service_name. "
        "Call list_incidents first if you do not yet know the exact service_name; never guess placeholders."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def search_recent_deployments(service_name: str, hours: int = 24) -> str:
    store = _get_store()
    deploys = store.search_recent_deployments(service_name)
    if deploys is None:
        return json.dumps(
            {
                "ok": False,
                "error": "SERVICE_NOT_FOUND",
                "known_services": store.list_known_services(),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "service_name": service_name,
            "lookback_hours": hours,
            "deployments": deploys,
        },
        ensure_ascii=False,
    )


@mcp.tool(
    description="Create a new internal IT/Ops incident or tracking ticket.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def create_ticket(
    title: str,
    service_name: str,
    severity: Literal["P1", "P2", "P3"],
    symptom: str,
) -> str:
    ticket = _get_store().create_ticket(
        title=title,
        service_name=service_name,
        severity=severity,
        symptom=symptom,
    )
    return json.dumps({"ok": True, "ticket": ticket}, ensure_ascii=False)


@mcp.tool(
    description="HIGH RISK: Restart a production service instance. Requires human approval.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
def restart_service(service_name: str, reason: str) -> str:
    result = _get_store().restart_service(service_name=service_name, reason=reason)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    description="HIGH RISK: Roll back a production service deployment to a prior version. Requires human approval.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
def rollback_deployment(service_name: str, target_version: str, reason: str) -> str:
    result = _get_store().rollback_deployment(
        service_name=service_name,
        target_version=target_version,
        reason=reason,
    )
    return json.dumps(result, ensure_ascii=False)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
