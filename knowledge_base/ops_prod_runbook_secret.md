---
title: 生产环境 Redis 应急切换与核心运维 Runbook（绝密）
department: sre_ops
allowed_roles: ops_admin
---

# 生产 Redis 主从切换与紧急扩容 Runbook

## 适用范围与权限红线
本手册仅限 `ops_admin` 值班工程师查阅，严禁向普通员工或外部人员披露内网管理地址与应急口令规则。

## redis-prod-01 应急操作步骤
1. 登录运维专用跳板机 `bastion-sre-01.prod.internal`（管理网段 `10.88.24.19`）。
2. 若确认由新发布版本连接泄漏引发，优先对上游 `order-api` 执行滚动回滚，禁止直接强杀 Redis 主进程。
3. 如确需临时扩容或触发 Sentinel 故障转移，需连接哨兵集群 `10.88.24.19:26379` 并填写应急变更单前缀 `OPS-BREAKGLASS-772`。
4. 切换完成后必须核对 `redis-prod-01` 主从复制延迟小于 100ms。
