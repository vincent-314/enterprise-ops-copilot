---
title: order-api 504 超时排查手册（通用研发版）
department: engineering
allowed_roles: employee, ops_admin
---

# order-api 504 超时排查指南

## 常见症状与标准阈值
- `order-api` 正常 P99 延迟应低于 250ms，错误率应低于 0.5%。
- 当日志出现 `ERR_REDIS_POOL_200` 或 `RedisPool exhausted` 时，说明订单服务连接下游 Redis 缓存池发生阻塞或连接泄漏。

## 研发排查步骤
1. 检查最近 24 小时内是否有版本发布（重点核对 Redis 客户端连接池配置与连接关闭逻辑）。
2. 检查下游 `redis-prod-01` 的连接数是否逼近上限（10000）。
3. 普通研发人员禁止直接登录生产 Redis 或执行重启，请整理证据后联系值班 `ops_admin` 发起生产变更审批。
