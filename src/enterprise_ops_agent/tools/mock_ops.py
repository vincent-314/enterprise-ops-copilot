"""Mock enterprise IT & Ops tools for Phase 1 multi-step runtime verification."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from enterprise_ops_agent.tools.registry import RiskLevel, ToolRegistry

_INCIDENTS = [
    {
        "ticket_id": "INC-2026-1042",
        "title": "订单支付接口间歇性 504 超时",
        "service": "order-api",
        "severity": "P1",
        "status": "open",
        "reported_at": "2026-11-18T09:12:00+08:00",
        "symptom": "过去 40 分钟内 P99 延迟从 120ms 飙升至 4800ms，伴随 Redis 连接超时日志。",
    },
    {
        "ticket_id": "INC-2026-1043",
        "title": "内部 Wiki 搜索慢",
        "service": "wiki-search",
        "severity": "P3",
        "status": "investigating",
        "reported_at": "2026-11-18T08:30:00+08:00",
        "symptom": "全文索引重建中，预计 15 分钟后恢复。",
    },
    {
        "ticket_id": "INC-2026-1039",
        "title": "办公网 VPN 证书续期",
        "service": "corp-vpn",
        "severity": "P3",
        "status": "closed",
        "reported_at": "2026-11-17T16:00:00+08:00",
        "symptom": "已完成证书轮换。",
    },
]

_METRICS = {
    "order-api": {
        "cpu_pct": 42.5,
        "memory_pct": 68.0,
        "error_rate_pct": 14.8,
        "p99_latency_ms": 4820,
        "downstream_dependency": "redis-prod-01",
        "recent_log_excerpt": "TimeoutError: RedisPool exhausted (max_connections=200, active=200)",
    },
    "redis-prod-01": {
        "cpu_pct": 89.2,
        "memory_pct": 74.5,
        "active_connections": 10000,
        "max_connections": 10000,
        "evicted_keys_per_sec": 0,
        "recent_log_excerpt": "max number of clients reached; rejecting new connections from order-api pods",
    },
    "wiki-search": {
        "cpu_pct": 61.0,
        "memory_pct": 55.0,
        "error_rate_pct": 0.2,
        "p99_latency_ms": 950,
    },
}

_DEPLOYMENTS = {
    "order-api": [
        {
            "version": "v2.14.3",
            "deployed_at": "2026-11-18T08:55:00+08:00",
            "author": "liwei",
            "summary": "新增订单缓存预热逻辑，将 Redis 客户端从全局单例改为按请求创建未关闭的连接池。",
        },
        {
            "version": "v2.14.2",
            "deployed_at": "2026-11-16T14:20:00+08:00",
            "author": "zhangmin",
            "summary": "修复发票抬头字段校验。",
        },
    ],
    "redis-prod-01": [],
}


class ListIncidentsArgs(BaseModel):
    status: Literal["open", "investigating", "closed", "all"] = Field(
        default="open",
        description="Filter incidents by status.",
    )


class ServiceNameArgs(BaseModel):
    service_name: str = Field(
        min_length=2,
        description="Exact service identifier, e.g. order-api or redis-prod-01.",
    )


class SearchDeploymentsArgs(BaseModel):
    service_name: str = Field(min_length=2, description="Target service identifier.")
    hours: int = Field(default=24, ge=1, le=168, description="Lookback window in hours.")


def _list_incidents(args: ListIncidentsArgs) -> str:
    matched = (
        _INCIDENTS
        if args.status == "all"
        else [item for item in _INCIDENTS if item["status"] == args.status]
    )
    return json.dumps({"ok": True, "count": len(matched), "incidents": matched}, ensure_ascii=False)


def _get_service_metrics(args: ServiceNameArgs) -> str:
    metrics = _METRICS.get(args.service_name)
    if metrics is None:
        return json.dumps(
            {
                "ok": False,
                "error": "SERVICE_NOT_FOUND",
                "known_services": sorted(_METRICS.keys()),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "service_name": args.service_name, "metrics": metrics},
        ensure_ascii=False,
    )


def _search_recent_deployments(args: SearchDeploymentsArgs) -> str:
    deploys = _DEPLOYMENTS.get(args.service_name)
    if deploys is None:
        return json.dumps(
            {
                "ok": False,
                "error": "SERVICE_NOT_FOUND",
                "known_services": sorted(_DEPLOYMENTS.keys()),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "service_name": args.service_name,
            "lookback_hours": args.hours,
            "deployments": deploys,
        },
        ensure_ascii=False,
    )


def build_mock_ops_registry() -> ToolRegistry:
    """Create a ToolRegistry populated with Phase 1 mock enterprise ops tools."""
    registry = ToolRegistry()
    registry.register(
        name="list_incidents",
        description="List internal IT/Ops incident tickets filtered by status.",
        args_model=ListIncidentsArgs,
        handler=_list_incidents,
        risk_level=RiskLevel.READ,
    )
    registry.register(
        name="get_service_metrics",
        description="Fetch live CPU, memory, latency, error rate, and recent log excerpt for a service.",
        args_model=ServiceNameArgs,
        handler=_get_service_metrics,
        risk_level=RiskLevel.READ,
    )
    registry.register(
        name="search_recent_deployments",
        description="Search recent release/deployment records for a service within a lookback window.",
        args_model=SearchDeploymentsArgs,
        handler=_search_recent_deployments,
        risk_level=RiskLevel.READ,
    )
    return registry
