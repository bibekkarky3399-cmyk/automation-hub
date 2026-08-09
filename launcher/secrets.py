"""Helpers for redacting sensitive workflow parameters."""

from __future__ import annotations

from typing import Any

_SENSITIVE_EXACT = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "client_secret",
    "clientsecret",
    "access_token",
    "refresh_token",
    "auth",
    "authorization",
    "private_key",
}

_SENSITIVE_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
)

REDACTED = "***"


def is_sensitive_key(key: str) -> bool:
    name = str(key or "").strip().lower().replace("-", "_")
    if not name:
        return False
    if name in _SENSITIVE_EXACT:
        return True
    return any(fragment in name for fragment in _SENSITIVE_FRAGMENTS)


def redact_parameters(parameters: dict[str, Any] | None, *, drop_empty: bool = True) -> dict[str, Any]:
    """Return a copy of parameters with sensitive values masked."""
    out: dict[str, Any] = {}
    for key, value in (parameters or {}).items():
        if drop_empty and value in (None, ""):
            continue
        if key == "max_image_bytes":
            continue
        out[key] = REDACTED if is_sensitive_key(str(key)) else value
    return out
