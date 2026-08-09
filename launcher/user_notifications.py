"""In-memory per-user notifications (e.g. background job finished)."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
# username (lower) → newest-first list
_inbox: dict[str, list[dict[str, Any]]] = {}
_MAX_PER_USER = 40


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(username: str) -> str:
    return (username or "").strip().lower()


def push_user_notification(
    username: str | None,
    *,
    title: str,
    body: str,
    level: str = "info",
    job_id: str | None = None,
    href: str | None = None,
    script_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    user = _key(username or "")
    if not user:
        return None
    item = {
        "id": str(uuid.uuid4()),
        "title": title,
        "body": body,
        "level": level,
        "job_id": job_id,
        "href": href or (f"/history/{job_id}" if job_id else "/jobs"),
        "script_name": script_name,
        "status": status,
        "created_at": _utc_now(),
        "read": False,
    }
    with _lock:
        bucket = _inbox.setdefault(user, [])
        bucket.insert(0, item)
        del bucket[_MAX_PER_USER:]
    return item


def list_user_notifications(
    username: str | None, *, unread_only: bool = False
) -> list[dict[str, Any]]:
    user = _key(username or "")
    if not user:
        return []
    with _lock:
        items = list(_inbox.get(user) or [])
    if unread_only:
        items = [i for i in items if not i.get("read")]
    return items


def ack_user_notifications(
    username: str | None, notification_ids: list[str] | None = None
) -> int:
    """Mark notifications read. If ids is None/empty, mark all unread as read."""
    user = _key(username or "")
    if not user:
        return 0
    wanted = {str(i) for i in (notification_ids or []) if i}
    marked = 0
    with _lock:
        for item in _inbox.get(user) or []:
            if item.get("read"):
                continue
            if wanted and item.get("id") not in wanted:
                continue
            item["read"] = True
            marked += 1
    return marked


def notify_starter_job_finished(entry: dict[str, Any], *, force: bool = False) -> None:
    """Push an inbox item when a background job finishes for its starter."""
    if not force and not entry.get("notify_on_complete"):
        return
    starter = entry.get("started_by")
    if not starter:
        return
    from launcher.run_naming import extract_run_name

    status = str(entry.get("status") or "")
    workflow = entry.get("script_name") or entry.get("script_id") or "Job"
    name = extract_run_name(run=entry, fallback=str(workflow))
    job_id = entry.get("job_id")
    workflow_note = f" ({workflow})" if name != workflow else ""
    if status == "success":
        title = f"{name} finished"
        body = f"Your background job completed successfully{workflow_note}."
        level = "success"
    elif status == "cancelled":
        title = f"{name} stopped"
        body = f"Your background job was cancelled{workflow_note}."
        level = "warning"
    elif status == "timeout":
        title = f"{name} timed out"
        body = f"Your background job took too long and stopped{workflow_note}."
        level = "danger"
    else:
        title = f"{name} didn’t finish"
        body = entry.get("error_message") or f"Your background job failed{workflow_note}."
        level = "danger"
    push_user_notification(
        starter,
        title=title,
        body=str(body)[:240],
        level=level,
        job_id=job_id,
        href=f"/history/{job_id}" if job_id else "/jobs",
        script_name=str(name),
        status=status,
    )
