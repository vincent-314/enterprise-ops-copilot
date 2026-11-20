"""Stateless HMAC-SHA256 Bearer token authentication for API gateway identity enforcement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Literal

from enterprise_ops_agent.tools.registry import UserContext

ALLOWED_ROLES: frozenset[str] = frozenset({"employee", "ops_admin", "sec_admin"})


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def issue_access_token(
    user_context: UserContext,
    *,
    secret: str,
    ttl_seconds: int = 86400,
) -> str:
    """Sign a stateless Bearer token encoding verified caller identity and role."""
    if user_context.role not in ALLOWED_ROLES:
        raise ValueError(f"Unsupported role '{user_context.role}'.")

    payload = {
        "sub": user_context.user_id,
        "role": user_context.role,
        "dept": user_context.department,
        "exp": int(time.time()) + ttl_seconds,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_access_token(token: str, *, secret: str) -> UserContext:
    """Verify HMAC signature and expiration, returning an immutable UserContext."""
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Malformed Bearer token.")

    payload_b64, sig_b64 = parts
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise ValueError("Invalid token signature encoding.") from exc

    if not hmac.compare_digest(actual_sig, expected_sig):
        raise ValueError("Invalid Bearer token signature.")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid Bearer token payload.") from exc

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise ValueError("Bearer token has expired.")

    user_id = str(payload.get("sub", "")).strip()
    role = str(payload.get("role", "")).strip()
    dept = str(payload.get("dept", "general")).strip() or "general"
    if not user_id or role not in ALLOWED_ROLES:
        raise ValueError("Token contains invalid identity claims.")

    return UserContext(user_id=user_id, role=role, department=dept)


def extract_bearer_token(authorization: str | None) -> str | None:
    """Extract Bearer token from an Authorization header if present."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise ValueError("Authorization header must use 'Bearer <token>' format.")
    return value.strip()


def can_review_high_risk(role: str) -> bool:
    """Only ops_admin and sec_admin may approve or reject HITL checkpoints."""
    return role in {"ops_admin", "sec_admin"}


DemoPersonaRole = Literal["employee", "ops_admin", "sec_admin"]
