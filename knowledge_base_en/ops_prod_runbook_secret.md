---
title: Production Redis Emergency Failover & Core Ops Runbook (Restricted)
department: sre_ops
allowed_roles: ops_admin
---

# Production Redis Failover & Emergency Scaling Runbook

## Scope & Access Boundary
This runbook is strictly restricted to on-duty `ops_admin` engineers. Never disclose internal management network addresses or break-glass credential prefixes to standard employees or external parties.

## redis-prod-01 Emergency Procedures
1. Connect via the dedicated SRE bastion host `bastion-sre-01.prod.internal` (management subnet `10.88.24.19`).
2. If the incident is caused by a connection leak from a recent release, prioritize rolling back upstream `order-api` rather than force-killing the primary Redis process.
3. If emergency scaling or Sentinel failover is required, connect to the Sentinel cluster at `10.88.24.19:26379` and open an emergency change ticket with prefix `OPS-BREAKGLASS-772`.
4. After failover, verify that `redis-prod-01` replication lag stays below 100ms.
