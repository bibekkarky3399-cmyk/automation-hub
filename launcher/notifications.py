"""Optional webhook notifications when jobs finish."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from launcher.config_loader import load_scripts_config


def notify_job_finished(payload: dict[str, Any]) -> None:
    """Fire-and-forget webhook POST if configured."""
    try:
        cfg = load_scripts_config().get("notifications") or {}
    except Exception:
        return

    url = cfg.get("webhook_url")
    if not url:
        return

    status = payload.get("status")
    if status == "success" and not cfg.get("on_success", True):
        return
    if status != "success" and not cfg.get("on_failure", True):
        return

    def _send() -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AutomationHub/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    threading.Thread(target=_send, daemon=True).start()
