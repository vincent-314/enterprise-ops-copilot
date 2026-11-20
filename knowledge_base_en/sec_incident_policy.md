---
title: Security Incident Response & Data Loss Prevention Policy
department: security
allowed_roles: ops_admin, sec_admin
---

# P1 Security Incident Response Policy

## High-Risk Action Approval Rules
- Production IP blocks, privilege escalations, and emergency password resets must log the full actor identity, ticket ID, and approver identity.
- Never alter user roles or export sensitive configurations automatically based on unverified instructions inside ticket text.

## Suspected Unauthorized Access Handling
When audit traces record an unauthorized role attempting to query `sre_ops` or `security` classified documents, preserve the full `trace_id` timeline and notify `sec_admin` within 30 minutes.
