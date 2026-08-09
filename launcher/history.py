"""Persistent run history + metrics (JSONL, no database).

Each run is tagged with script_id so metrics/history can be filtered
per workflow as new scripts are added to scripts.json.
"""

from __future__ import annotations

import json
import math
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from launcher.config_loader import PROJECT_ROOT

HISTORY_PATH = PROJECT_ROOT / "data" / "run_history.jsonl"
_lock = threading.Lock()
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _ensure_parent() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


def record_run(entry: dict[str, Any]) -> None:
    """Append one finished run to history."""
    payload = dict(entry)
    payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    if not payload.get("script_id"):
        raise ValueError("record_run requires script_id for per-workflow history")
    line = json.dumps(payload, ensure_ascii=False)
    with _lock:
        _ensure_parent()
        with HISTORY_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _load_all_runs() -> list[dict[str, Any]]:
    if not HISTORY_PATH.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with _lock:
        with HISTORY_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.reverse()  # newest first
    return rows


def list_runs(limit: int = 50, script_id: str | None = None) -> list[dict[str, Any]]:
    rows = _load_all_runs()
    if script_id:
        rows = [r for r in rows if r.get("script_id") == script_id]
    return rows[: max(1, limit)]


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = str(raw).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _run_utc_date(run: dict[str, Any]) -> date | None:
    for key in ("finished_at", "recorded_at", "created_at", "started_at"):
        raw = run.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).date()
        except ValueError:
            continue
    return None


def resolve_runs_date_window(
    *,
    range_key: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, date | None, date | None]:
    """Return (preset, from_date, to_date). Default preset is today (UTC)."""
    today = datetime.now(timezone.utc).date()
    explicit_from = _parse_iso_date(date_from)
    explicit_to = _parse_iso_date(date_to)
    key = (range_key or "").strip().lower()

    if explicit_from or explicit_to:
        start = explicit_from or explicit_to
        end = explicit_to or explicit_from
        assert start is not None and end is not None
        if start > end:
            start, end = end, start
        return "custom", start, end

    if key in {"", "today"}:
        return "today", today, today
    if key in {"7d", "week", "last7"}:
        return "7d", today - timedelta(days=6), today
    if key == "all":
        return "all", None, None
    # Unknown preset → today
    return "today", today, today


def _run_matches_query(run: dict[str, Any], query: str) -> bool:
    """Case-insensitive match across job id, names, error, params, and filenames."""
    q = (query or "").strip().lower()
    if not q:
        return True
    haystacks: list[str] = [
        str(run.get("job_id") or ""),
        str(run.get("script_id") or ""),
        str(run.get("script_name") or ""),
        str(run.get("run_name") or ""),
        str(run.get("error_message") or ""),
        str(run.get("status") or ""),
        str(run.get("started_by") or ""),
    ]
    params = run.get("parameters") or {}
    if isinstance(params, dict):
        for key, value in params.items():
            haystacks.append(str(key))
            haystacks.append(str(value))
    for artifact in run.get("artifacts") or []:
        if isinstance(artifact, dict):
            haystacks.extend(
                str(artifact.get(k) or "")
                for k in ("csv_name", "csv_path", "source_image", "filename")
            )
    for output in run.get("outputs") or []:
        if isinstance(output, dict):
            haystacks.extend(
                str(output.get(k) or "") for k in ("filename", "label", "path")
            )
    report = run.get("report_path")
    if report:
        haystacks.append(str(report))
    blob = " ".join(haystacks).lower()
    return q in blob


def query_runs(
    *,
    script_id: str | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Filter run history and return one page (newest first)."""
    rows = _load_all_runs()
    if script_id:
        rows = [r for r in rows if r.get("script_id") == script_id]

    status_key = (status or "").strip().lower()
    if status_key == "success":
        rows = [r for r in rows if r.get("status") == "success"]
    elif status_key == "failed":
        rows = [r for r in rows if r.get("status") == "failed"]
    elif status_key == "cancelled":
        rows = [r for r in rows if r.get("status") == "cancelled"]
    elif status_key == "timeout":
        rows = [r for r in rows if r.get("status") == "timeout"]
    elif status_key in {"unfinished", "error", "didnt_finish", "didn’t_finish"}:
        rows = [
            r for r in rows if r.get("status") in {"failed", "cancelled", "timeout"}
        ]

    query = (q or "").strip()
    if query:
        rows = [r for r in rows if _run_matches_query(r, query)]

    if date_from is not None or date_to is not None:
        start = date_from
        end = date_to
        filtered: list[dict[str, Any]] = []
        for run in rows:
            day = _run_utc_date(run)
            if day is None:
                continue
            if start is not None and day < start:
                continue
            if end is not None and day > end:
                continue
            filtered.append(run)
        rows = filtered

    total = len(rows)
    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    pages = max(1, math.ceil(total / size)) if total else 1
    page_n = max(1, min(int(page or 1), pages))
    start_i = (page_n - 1) * size
    page_rows = rows[start_i : start_i + size]

    return {
        "runs": page_rows,
        "total": total,
        "page": page_n,
        "page_size": size,
        "pages": pages,
        "has_prev": page_n > 1,
        "has_next": page_n < pages,
    }


def build_runs_query(**params: Any) -> str:
    """Stable query string for runs page links (skips empty values)."""
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        cleaned[key] = value
    return urlencode(cleaned)


_STATUS_LABELS = {
    "success": "Worked",
    "failed": "Didn't finish",
    "cancelled": "Stopped early",
    "timeout": "Took too long",
    "running": "Still running",
    "queued": "Waiting to start",
}


def _parse_run_dt(run: dict[str, Any]) -> datetime | None:
    for key in ("finished_at", "recorded_at", "created_at", "started_at"):
        raw = run.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _format_duration(seconds: Any) -> str | None:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    whole = int(round(total))
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _humanize_param_id(param_id: str) -> str:
    text = str(param_id or "").replace("_", " ").replace("-", " ").strip()
    return text.title() if text else "Setting"


def _input_label_map(script_id: str | None) -> dict[str, str]:
    """Map input ids → form labels from the live workflow/pipeline config."""
    sid = str(script_id or "").strip()
    if not sid:
        return {}
    try:
        from launcher.config_loader import get_pipeline_by_id, get_script_by_id

        if sid.startswith("pipeline:"):
            pipeline = get_pipeline_by_id(sid.split(":", 1)[1], compose_inputs=True)
            inputs = pipeline.get("inputs") or []
        else:
            inputs = (get_script_by_id(sid).get("inputs") or [])
    except Exception:
        return {}

    labels: dict[str, str] = {}
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        inp_id = str(inp.get("id") or "").strip()
        if not inp_id:
            continue
        label = str(inp.get("label") or "").strip()
        labels[inp_id] = label or _humanize_param_id(inp_id)
    return labels


def labeled_parameter_rows(
    parameters: dict[str, Any] | None, script_id: str | None = None
) -> list[dict[str, Any]]:
    """Return parameters as [{id, label, value}] using workflow input labels."""
    labels = _input_label_map(script_id)
    rows: list[dict[str, Any]] = []
    for key, value in (parameters or {}).items():
        if value in (None, ""):
            continue
        param_id = str(key)
        if param_id == "run_name":
            # Shown as the run title; keep out of the settings dump.
            continue
        rows.append(
            {
                "id": param_id,
                "label": labels.get(param_id) or _humanize_param_id(param_id),
                "value": value,
            }
        )
    return rows


def decorate_runs_for_display(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add plain-language fields for the History timeline UI."""
    from launcher.today_desk import summarize_run_business

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    decorated: list[dict[str, Any]] = []

    for run in runs:
        item = dict(run)
        status = str(run.get("status") or "unknown").lower()
        item["status_key"] = status
        item["status_label"] = _STATUS_LABELS.get(status, status.replace("_", " ").title())
        item["duration_label"] = _format_duration(run.get("duration_seconds"))
        item["parameter_rows"] = labeled_parameter_rows(
            run.get("parameters"), run.get("script_id")
        )

        dt = _parse_run_dt(run)
        if dt is None:
            item["day_key"] = ""
            item["day_label"] = "Unknown day"
            item["time_label"] = "—"
            item["when_title"] = ""
        else:
            day = dt.date()
            item["day_key"] = day.isoformat()
            if day == today:
                item["day_label"] = "Today"
            elif day == yesterday:
                item["day_label"] = "Yesterday"
            else:
                try:
                    item["day_label"] = dt.strftime("%A, %b %-d")
                except ValueError:
                    item["day_label"] = dt.strftime("%A, %b %d").replace(" 0", " ")
            item["time_label"] = dt.strftime("%H:%M")
            item["when_title"] = dt.strftime("%Y-%m-%d %H:%M UTC")

        biz = summarize_run_business(run)
        item["business"] = biz
        item["outcome_line"] = biz.get("line") or ""
        item["outcome_headline"] = biz.get("headline") or ""

        from launcher.run_naming import attach_run_display_fields

        attach_run_display_fields(item)

        decorated.append(item)
    return decorated


def get_run(job_id: str) -> dict[str, Any] | None:
    for run in _load_all_runs():
        if run.get("job_id") == job_id:
            return run
    return None


def _relative_when(dt: datetime | None) -> str:
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    seconds = int(max(0, (now - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = seconds // 60
        return f"{mins}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return dt.strftime("%b %d")


def latest_run_summaries_by_script() -> dict[str, dict[str, Any]]:
    """Newest finished run per script_id for home cards."""
    latest: dict[str, dict[str, Any]] = {}
    for run in _load_all_runs():
        sid = str(run.get("script_id") or "")
        if not sid or sid in latest:
            continue
        status = str(run.get("status") or "").lower()
        dt = _parse_run_dt(run)
        latest[sid] = {
            "job_id": run.get("job_id"),
            "status": status,
            "status_label": _STATUS_LABELS.get(status, status.replace("_", " ").title()),
            "when_label": _relative_when(dt),
            "started_by": run.get("started_by") or "",
        }
    return latest


def build_home_ops(
    *,
    current_user: str | None = None,
    running_count: int = 0,
) -> dict[str, Any]:
    """Compact ops strip for the Workflows home page."""
    today = datetime.now(timezone.utc).date()
    failed_today = 0
    last_for_user: dict[str, Any] | None = None
    last_any: dict[str, Any] | None = None
    user = (current_user or "").strip().lower()
    today_complete = False

    for run in _load_all_runs():
        if last_any is None:
            last_any = run

        day = _run_utc_date(run)
        status = str(run.get("status") or "").lower()

        if not today_complete:
            if day == today and status in {"failed", "cancelled", "timeout"}:
                failed_today += 1
            elif day is not None and day < today:
                today_complete = True

        if user and last_for_user is None:
            starter = str(run.get("started_by") or "").strip().lower()
            if starter == user:
                last_for_user = run

        if today_complete and (last_for_user is not None or not user):
            break

    pick = last_for_user or last_any
    last_run = None
    if pick:
        status = str(pick.get("status") or "").lower()
        dt = _parse_run_dt(pick)
        from launcher.run_naming import attach_run_display_fields

        last_run = attach_run_display_fields(
            {
                "job_id": pick.get("job_id"),
                "script_id": pick.get("script_id"),
                "script_name": pick.get("script_name") or pick.get("script_id") or "Job",
                "run_name": pick.get("run_name")
                or (pick.get("parameters") or {}).get("run_name"),
                "status": status,
                "status_label": _STATUS_LABELS.get(status, status.replace("_", " ").title()),
                "when_label": _relative_when(dt),
                "started_by": pick.get("started_by") or "",
                "is_mine": bool(
                    user and str(pick.get("started_by") or "").strip().lower() == user
                ),
                "href": f"/history/{pick.get('job_id')}" if pick.get("job_id") else "/history",
            }
        )

    return {
        "running_count": int(running_count or 0),
        "failed_today": failed_today,
        "last_run": last_run,
    }


def list_script_ids_in_history() -> list[str]:
    """Distinct script_ids seen in history (includes retired workflows)."""
    seen: dict[str, str] = {}
    for run in _load_all_runs():
        sid = run.get("script_id")
        if sid and sid not in seen:
            seen[sid] = run.get("script_name") or sid
    return list(seen.keys())


def _metrics_for_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(runs)
    success = sum(1 for r in runs if r.get("status") == "success")
    failed = sum(1 for r in runs if r.get("status") in {"failed", "cancelled", "timeout"})
    durations = [float(r["duration_seconds"]) for r in runs if r.get("duration_seconds")]
    images = [int(r["images_processed"]) for r in runs if r.get("images_processed")]
    rows = [int(r["rows_total"]) for r in runs if r.get("rows_total") is not None]

    by_day: dict[str, int] = defaultdict(int)
    by_day_status: dict[str, dict[str, int]] = defaultdict(
        lambda: {"success": 0, "failed": 0, "other": 0}
    )
    booked_vals: list[int] = []
    for r in runs:
        ts = r.get("finished_at") or r.get("recorded_at") or ""
        day = ts[:10] if len(ts) >= 10 else "unknown"
        by_day[day] += 1
        status = r.get("status") or "other"
        if status == "success":
            by_day_status[day]["success"] += 1
        elif status in {"failed", "cancelled", "timeout"}:
            by_day_status[day]["failed"] += 1
        else:
            by_day_status[day]["other"] += 1
        summary = r.get("summary") or {}
        params = r.get("parameters") or {}
        # PNR booked count may live in summary from live jobs; history stores top-level fields
        booked = summary.get("booked") or r.get("booked")
        if booked is not None:
            try:
                booked_vals.append(int(booked))
            except (TypeError, ValueError):
                pass
        # Derive a short ops summary for history cards when possible
        if "origin" in params and "destination" in params:
            pass  # consumed by UI via parameters

    day_keys = sorted(by_day.keys())[-14:]
    runs_by_day_series = [
        {
            "day": d,
            "total": by_day[d],
            "success": by_day_status[d]["success"],
            "failed": by_day_status[d]["failed"],
        }
        for d in day_keys
    ]

    avg_duration = round(sum(durations) / len(durations), 2) if durations else None
    avg_images = round(sum(images) / len(images), 2) if images else None
    per_image = None
    if durations and images and sum(images) > 0:
        per_image = round(sum(durations) / sum(images), 2)
    avg_rows = round(sum(rows) / len(rows), 2) if rows else None
    avg_booked = round(sum(booked_vals) / len(booked_vals), 2) if booked_vals else None

    return {
        "runs_total": total,
        "runs_success": success,
        "runs_failed": failed,
        "success_rate": round(success / total, 3) if total else None,
        "avg_duration_seconds": avg_duration,
        "avg_images_per_run": avg_images,
        "avg_seconds_per_image": per_image,
        "avg_rows_per_run": avg_rows,
        "avg_booked_per_run": avg_booked,
        "runs_by_day": dict((d, by_day[d]) for d in day_keys),
        "runs_by_day_series": runs_by_day_series,
        "recent": runs[:10],
    }


def compute_metrics(script_id: str | None = None) -> dict[str, Any]:
    """Metrics for one workflow, or global if script_id is None."""
    runs = list_runs(limit=10_000, script_id=script_id)
    result = _metrics_for_runs(runs)
    result["script_id"] = script_id
    return result


_PIE_WORKFLOW_COLORS = (
    "#2dd4bf",
    "#38bdf8",
    "#67e8f9",
    "#a3e635",
    "#fbbf24",
    "#fb7185",
)


def build_outcome_pie(metrics: dict[str, Any]) -> dict[str, Any]:
    """Pie chart data for success / failed / other run outcomes."""
    ok = int(metrics.get("runs_success") or 0)
    fail = int(metrics.get("runs_failed") or 0)
    total = int(metrics.get("runs_total") or 0)
    other = max(0, total - ok - fail)
    if total <= 0:
        return {"total": 0, "gradient": "", "slices": []}

    slices = []
    cursor = 0.0
    for label, count, color, swatch in (
        ("Finished OK", ok, "var(--app-ok)", "legend-ok"),
        ("Did not finish", fail, "var(--app-fail)", "legend-fail"),
        ("Still running / other", other, "color-mix(in srgb, var(--app-muted) 55%, var(--app-elevated))", "legend-other"),
    ):
        if count <= 0:
            continue
        pct = round((count / total) * 100, 1)
        start = cursor
        cursor = min(100.0, cursor + pct)
        slices.append(
            {
                "label": label,
                "count": count,
                "pct": pct,
                "start": start,
                "end": cursor,
                "color": color,
                "swatch": swatch,
            }
        )
    if slices:
        slices[-1]["end"] = 100.0
    gradient = ", ".join(f"{s['color']} {s['start']}% {s['end']}%" for s in slices)
    return {"total": total, "gradient": gradient, "slices": slices}


def build_day_graph(series: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Calendar-style heatmap data for runs-by-day metrics."""
    from datetime import date, timedelta

    days = list(series or [])
    if not days:
        return None

    by_day: dict[str, dict[str, Any]] = {}
    for item in days:
        key = str(item.get("day") or "")
        if not key:
            continue
        by_day[key] = {
            "total": int(item.get("total") or 0),
            "success": int(item.get("success") or 0),
            "failed": int(item.get("failed") or 0),
        }

    parsed: list[date] = []
    for key in by_day:
        try:
            parsed.append(date.fromisoformat(key))
        except ValueError:
            continue
    if not parsed:
        return None

    start = min(parsed)
    end = max(parsed)
    # Align calendar to full weeks (Mon–Sun)
    grid_start = start - timedelta(days=start.weekday())
    grid_end = end + timedelta(days=(6 - end.weekday()))

    max_total = max((row["total"] for row in by_day.values()), default=0) or 1
    cells: list[dict[str, Any]] = []
    cursor = grid_start
    while cursor <= grid_end:
        key = cursor.isoformat()
        row = by_day.get(key)
        total = int(row["total"]) if row else 0
        success = int(row["success"]) if row else 0
        failed = int(row["failed"]) if row else 0
        in_range = start <= cursor <= end
        intensity = 0 if total <= 0 else max(1, min(4, round((total / max_total) * 4)))
        cells.append(
            {
                "day": key,
                "day_num": cursor.day,
                "month_label": cursor.strftime("%b"),
                "weekday": cursor.strftime("%a"),
                "weekday_index": cursor.weekday(),
                "total": total,
                "success": success,
                "failed": failed,
                "rate": round((success / total) * 100) if total else 0,
                "intensity": intensity,
                "in_range": in_range,
                "is_today": cursor == date.today(),
                "has_data": bool(row),
            }
        )
        cursor += timedelta(days=1)

    weeks: list[list[dict[str, Any]]] = []
    for index in range(0, len(cells), 7):
        weeks.append(cells[index : index + 7])

    if start.month == end.month and start.year == end.year:
        month_title = start.strftime("%B %Y")
    else:
        month_title = (
            f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
        )

    success_sum = sum(row["success"] for row in by_day.values())
    failed_sum = sum(row["failed"] for row in by_day.values())
    total_sum = sum(row["total"] for row in by_day.values())

    return {
        "weeks": weeks,
        "weekday_headers": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "month_title": month_title,
        "start_label": start.isoformat(),
        "end_label": end.isoformat(),
        "day_count": len(by_day),
        "success_sum": success_sum,
        "failed_sum": failed_sum,
        "total_sum": total_sum,
        "success_rate": round((success_sum / total_sum) * 100, 1) if total_sum else 0,
        "peak": max_total,
    }


def build_workflow_pie(workflows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pie chart data for run volume by workflow."""
    active = []
    for index, wf in enumerate(workflows or []):
        count = int((wf.get("metrics") or {}).get("runs_total") or 0)
        if count <= 0:
            continue
        active.append((wf, count, index))
    total = sum(count for _, count, _ in active)
    if total <= 0:
        return {"total": 0, "gradient": "", "slices": []}

    slices = []
    cursor = 0.0
    for wf, count, index in active:
        pct = round((count / total) * 100, 1)
        start = cursor
        cursor = min(100.0, cursor + pct)
        color = _PIE_WORKFLOW_COLORS[index % len(_PIE_WORKFLOW_COLORS)]
        slices.append(
            {
                "label": wf.get("script_name") or wf.get("script_id") or "Workflow",
                "count": count,
                "pct": round((count / total) * 100),
                "start": start,
                "end": cursor,
                "color": color,
                "swatch_index": index % len(_PIE_WORKFLOW_COLORS),
            }
        )
    if slices:
        slices[-1]["end"] = 100.0
    gradient = ", ".join(f"{s['color']} {s['start']}% {s['end']}%" for s in slices)
    return {"total": total, "gradient": gradient, "slices": slices}


def compute_metrics_by_workflow(
    known_scripts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Per-workflow metrics keyed by script_id.

    Includes every script currently in scripts.json plus any script_id
    that still appears in history (retired workflows).
    """
    all_runs = _load_all_runs()
    by_script: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in all_runs:
        sid = run.get("script_id") or "unknown"
        by_script[sid].append(run)

    catalog: dict[str, dict[str, Any]] = {}
    for s in known_scripts or []:
        catalog[s["id"]] = {
            "script_id": s["id"],
            "script_name": s.get("name") or s["id"],
            "description": s.get("description") or "",
            "badge": s.get("badge") or "",
            "enabled": bool(s.get("enabled", True)),
            "icon": s.get("icon") or "bi-terminal",
        }

    # Retired / unknown ids from history
    for sid, runs in by_script.items():
        if sid not in catalog:
            catalog[sid] = {
                "script_id": sid,
                "script_name": runs[0].get("script_name") or sid,
                "description": "",
                "badge": "Retired",
                "enabled": False,
                "icon": "bi-archive",
            }

    workflows = []
    for sid, meta in catalog.items():
        metrics = _metrics_for_runs(by_script.get(sid, []))
        workflows.append({**meta, "metrics": metrics})

    # Active / configured first, then by run volume
    workflows.sort(
        key=lambda w: (
            0 if w.get("enabled") else 1,
            -(w["metrics"].get("runs_total") or 0),
            w.get("script_name") or "",
        )
    )

    overall = _metrics_for_runs(all_runs)
    return {
        "overall": overall,
        "workflows": workflows,
    }
