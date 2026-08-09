"""Locate HTML reports produced by scripts without parsing report contents."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from launcher.config_loader import PROJECT_ROOT


def find_report_for_job(
    script: dict[str, Any],
    stdout_text: str,
    started_at_iso: str | None,
) -> Path | None:
    report_cfg = script.get("report")
    if not report_cfg:
        return None

    # Prefer explicit path printed by the script (OCR prints "Report: /path")
    prefix = report_cfg.get("stdout_line_prefix", "Report:")
    for line in stdout_text.splitlines():
        if line.strip().startswith(prefix):
            raw = line.split(":", 1)[1].strip()
            path = Path(raw)
            if path.is_file():
                return path

    search_dir = PROJECT_ROOT / report_cfg.get("search_dir", "output")
    glob_pattern = report_cfg.get("glob", "*.html")
    if not search_dir.is_dir():
        return None

    candidates = list(search_dir.glob(glob_pattern))
    if not candidates:
        return None

    pick = report_cfg.get("pick", "newest")
    if pick == "newest_after_start" and started_at_iso:
        try:
            started = datetime.fromisoformat(started_at_iso)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except ValueError:
            started = None
        if started:
            filtered = [p for p in candidates if _mtime_utc(p) >= started]
            if filtered:
                candidates = filtered

    return max(candidates, key=lambda p: p.stat().st_mtime)


def _mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
