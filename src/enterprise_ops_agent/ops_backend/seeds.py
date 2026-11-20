"""Shared locale-aware seed fixtures for SQLite and PostgreSQL OpsStore backends."""

from __future__ import annotations

from typing import Any, Literal


def get_seed_incidents(locale: Literal["en", "zh"] = "en") -> list[tuple[str, str, str, str, str, str, str]]:
    """Return initial incident rows in English or Chinese."""
    if locale == "zh":
        return [
            (
                "INC-2026-1042",
                "订单支付接口间歇性 504 超时",
                "order-api",
                "P1",
                "open",
                "2026-11-18T09:12:00+08:00",
                "过去 40 分钟内 P99 延迟从 120ms 飙升至 4800ms，伴随 ERR_REDIS_POOL_200 超时日志。",
            ),
            (
                "INC-2026-1043",
                "内部 Wiki 搜索慢",
                "wiki-search",
                "P3",
                "investigating",
                "2026-11-18T08:30:00+08:00",
                "全文索引重建中，预计 15 分钟后恢复。",
            ),
            (
                "INC-2026-1039",
                "办公网 VPN 证书续期",
                "corp-vpn",
                "P3",
                "closed",
                "2026-11-17T16:00:00+08:00",
                "已完成证书轮换。",
            ),
        ]
    return [
        (
            "INC-2026-1042",
            "Intermittent 504 Gateway Timeouts on Order Checkout API",
            "order-api",
            "P1",
            "open",
            "2026-11-18T09:12:00+08:00",
            "P99 latency spiked from 120ms to 4800ms over the past 40 minutes with ERR_REDIS_POOL_200 logs.",
        ),
        (
            "INC-2026-1043",
            "Slow Internal Wiki Search Latency",
            "wiki-search",
            "P3",
            "investigating",
            "2026-11-18T08:30:00+08:00",
            "Full-text index rebuild in progress; expected recovery in 15 minutes.",
        ),
        (
            "INC-2026-1039",
            "Corporate VPN Certificate Rotation",
            "corp-vpn",
            "P3",
            "closed",
            "2026-11-17T16:00:00+08:00",
            "TLS certificate rotation completed.",
        ),
    ]


def get_seed_services() -> list[tuple[str, str, str, dict[str, Any]]]:
    """Return baseline service health metrics."""
    return [
        (
            "order-api",
            "v2.14.3",
            "degraded",
            {
                "cpu_pct": 42.5,
                "memory_pct": 68.0,
                "error_rate_pct": 14.8,
                "p99_latency_ms": 4820,
                "downstream_dependency": "redis-prod-01",
                "recent_log_excerpt": "ERR_REDIS_POOL_200: RedisPool exhausted (max_connections=200, active=200)",
            },
        ),
        (
            "redis-prod-01",
            "7.2.4",
            "degraded",
            {
                "cpu_pct": 89.2,
                "memory_pct": 74.5,
                "active_connections": 10000,
                "max_connections": 10000,
                "evicted_keys_per_sec": 0,
                "recent_log_excerpt": "max number of clients reached; rejecting new connections from order-api pods",
            },
        ),
        (
            "wiki-search",
            "v1.8.0",
            "degraded",
            {
                "cpu_pct": 61.0,
                "memory_pct": 55.0,
                "error_rate_pct": 0.2,
                "p99_latency_ms": 950,
            },
        ),
    ]


def get_seed_deployments(locale: Literal["en", "zh"] = "en") -> list[tuple[str, str, str, str, str]]:
    """Return recent deployment history rows."""
    if locale == "zh":
        return [
            (
                "order-api",
                "v2.14.3",
                "2026-11-18T08:55:00+08:00",
                "liwei",
                "新增订单缓存预热逻辑，将 Redis 客户端从全局单例改为按请求创建未关闭的连接池。",
            ),
            (
                "order-api",
                "v2.14.2",
                "2026-11-16T14:20:00+08:00",
                "zhangmin",
                "修复发票抬头字段校验（稳定版本）。",
            ),
        ]
    return [
        (
            "order-api",
            "v2.14.3",
            "2026-11-18T08:55:00+08:00",
            "liwei",
            "Added order cache pre-warming; refactored Redis client from singleton to per-request unclosed pool.",
        ),
        (
            "order-api",
            "v2.14.2",
            "2026-11-16T14:20:00+08:00",
            "zhangmin",
            "Fixed invoice header field validation (stable baseline).",
        ),
    ]
