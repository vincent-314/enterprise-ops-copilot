---
title: Corporate VPN & Account Security Self-Service Guide
department: it_support
allowed_roles: employee, ops_admin, sec_admin
---

# Corporate VPN Troubleshooting

## Common Error Codes
- Error `VPN-401`: Usually caused by an expired corporate SSO password or an out-of-sync MFA token. Reset your password on the internal self-service portal, wait 2 minutes, and reconnect.
- Error `VPN-503`: Primary gateway saturation. Switch to the backup gateway endpoint `vpn-sg-02.corp.internal` in your VPN client.

## MFA Device Replacement Procedure
1. When replacing your mobile device invalidates your OTP token, submit ticket type `MFA-RESET-01` via the IT Self-Service Portal.
2. Once approved by your manager, the IT Service Desk delivers a one-time enrollment QR code within 15 minutes.
