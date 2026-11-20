---
title: order-api 504 Timeout Troubleshooting Guide (Engineering)
department: engineering
allowed_roles: employee, ops_admin
---

# order-api 504 Timeout Diagnostic Manual

## Baseline Thresholds & Symptoms
- Healthy `order-api` P99 latency should remain below 250ms with an error rate under 0.5%.
- Log entries containing `ERR_REDIS_POOL_200` or `RedisPool exhausted` indicate connection pool saturation or leaks against downstream Redis.

## Engineering Triage Steps
1. Check deployments within the past 24 hours (specifically inspect Redis client pool sizing and connection closing logic).
2. Check whether downstream `redis-prod-01` active connections are approaching the hard limit (`10000`).
3. Standard engineers are prohibited from accessing production Redis directly or triggering restarts; gather diagnostic evidence and escalate to on-duty `ops_admin` for human-approved remediation.
