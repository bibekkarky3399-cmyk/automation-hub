"""Hub auth accounts: admin and operator."""

from __future__ import annotations

import os
import secrets
from typing import Any


ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_OPERATOR})


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load_hub_users() -> list[dict[str, str]]:
    """Return configured Hub users (username, password, role).

    Defaults:
      admin / admin → admin
      operator / operator → operator

    Legacy HUB_USERNAME / HUB_PASSWORD still map to the admin account.
    """
    admin_user = _env("HUB_ADMIN_USERNAME") or _env("HUB_USERNAME", "admin") or "admin"
    admin_pass = _env("HUB_ADMIN_PASSWORD") or _env("HUB_PASSWORD", "admin") or "admin"
    operator_user = _env("HUB_OPERATOR_USERNAME", "operator") or "operator"
    operator_pass = _env("HUB_OPERATOR_PASSWORD", "operator") or "operator"

    users = [
        {"username": admin_user, "password": admin_pass, "role": ROLE_ADMIN},
        {"username": operator_user, "password": operator_pass, "role": ROLE_OPERATOR},
    ]

    # Drop duplicate usernames (first wins — admin, then operator).
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for user in users:
        key = user["username"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(user)
    return unique


def authenticate(username: str, password: str) -> dict[str, str] | None:
    """Validate credentials; return {username, role} or None."""
    candidate = (username or "").strip()
    secret = password or ""
    if not candidate or not secret:
        return None

    for user in load_hub_users():
        expected_user = user["username"]
        expected_pass = user["password"]
        user_ok = (
            secrets.compare_digest(candidate.encode("utf-8"), expected_user.encode("utf-8"))
            if len(candidate) == len(expected_user)
            else False
        )
        pass_ok = (
            secrets.compare_digest(secret.encode("utf-8"), expected_pass.encode("utf-8"))
            if len(secret) == len(expected_pass)
            else False
        )
        if user_ok and pass_ok:
            return {"username": expected_user, "role": user["role"]}
    return None


def normalize_role(role: Any) -> str:
    value = str(role or "").strip().lower()
    if value == "viewer":
        # Retired role — treat as operator for existing sessions.
        return ROLE_OPERATOR
    return value if value in VALID_ROLES else ROLE_OPERATOR


def is_admin_role(role: Any) -> bool:
    return normalize_role(role) == ROLE_ADMIN


def user_can_cancel_job(username: str | None, started_by: str | None) -> bool:
    """Only the account that started the job may stop it."""
    starter = (started_by or "").strip()
    actor = (username or "").strip()
    if not starter or not actor:
        return False
    return starter.lower() == actor.lower()
