---
title: 办公网 VPN 与账号安全自助指南
department: it_support
allowed_roles: employee, ops_admin, sec_admin
---

# 办公网 VPN 连接问题

## 常见报错处理
- 错误码 `VPN-401`：通常由于企业 SSO 密码过期或 MFA 令牌未同步导致。请前往内网自助门户重置密码后等待 2 分钟重试。
- 错误码 `VPN-503`：华东/华北网关负载过高，可在客户端切换备用接入点 `vpn-bj-02.corp.internal`。

## MFA 手机更换流程
1. 员工更换手机导致动态口令失效时，需在 IT 自助台提交工单类型 `MFA-RESET-01`。
2. 主管审批通过后，IT 服务台会在 15 分钟内下发一次性绑定二维码。
