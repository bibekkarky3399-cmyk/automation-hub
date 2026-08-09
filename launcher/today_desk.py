"""Airline ops desk: today's board, business outcome lines, CSV day reports."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from launcher.history import (
    _STATUS_LABELS,
    _load_all_runs,
    _parse_run_dt,
    _relative_when,
    _run_utc_date,
)

_MAX_LIST = 6
_MAX_CSV_SCAN_ROWS = 5000


def _reuse_href(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    sid = str(run.get("script_id") or "")
    jid = str(run.get("job_id") or "")
    if not sid or not jid:
        return ""
    if sid.startswith("pipeline:"):
        return f"/pipeline/{sid.split(':', 1)[1]}?reuse={jid}"
    return f"/runner/{sid}?reuse={jid}"


def _primary_download(run: dict[str, Any]) -> str:
    jid = str(run.get("job_id") or "")
    if not jid:
        return ""
    outputs = run.get("outputs") or []
    if outputs and isinstance(outputs[0], dict) and outputs[0].get("download_url"):
        return str(outputs[0]["download_url"])
    if run.get("report_path") or run.get("artifacts"):
        return f"/report/{jid}/download"
    return ""


def _param(run: dict[str, Any], *keys: str) -> str:
    params = run.get("parameters") or {}
    for key in keys:
        val = params.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _business_kind(script_id: str | None) -> str:
    sid = (script_id or "").lower()
    if sid.startswith("pipeline:"):
        return "chain"
    if "ocr" in sid:
        return "ocr"
    if "pnr" in sid:
        return "pnr"
    if "booking" in sid or sid.endswith("_api") or "api" in sid:
        return "api"
    return "other"


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_csv_int(path: Path, columns: list[str]) -> dict[str, int]:
    totals = {c: 0 for c in columns}
    if not path.is_file():
        return totals
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader):
                if i >= _MAX_CSV_SCAN_ROWS:
                    break
                for col in columns:
                    raw = (row.get(col) or "").strip()
                    if not raw:
                        continue
                    try:
                        totals[col] += int(float(raw))
                    except ValueError:
                        continue
    except OSError:
        pass
    return totals


def _count_pnr_csv(path: Path) -> dict[str, int]:
    result = {"booked": 0, "failed": 0, "rows": 0, "with_pnr": 0}
    if not path.is_file():
        return result
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader):
                if i >= _MAX_CSV_SCAN_ROWS:
                    break
                result["rows"] += 1
                status = (row.get("status") or "").strip().lower()
                pnr = (row.get("pnr") or "").strip()
                if pnr:
                    result["with_pnr"] += 1
                if status in {"booked", "success", "ok", "confirmed"} or (
                    pnr and status not in {"failed", "error", "fail"}
                ):
                    result["booked"] += 1
                elif status in {"failed", "error", "fail", "rejected"}:
                    result["failed"] += 1
    except OSError:
        pass
    return result


def _resolve_csv_path(run: dict[str, Any]) -> Path | None:
    for art in run.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        raw = art.get("csv_path") or ""
        if raw:
            path = Path(str(raw))
            if path.is_file():
                return path
    report = run.get("report_path")
    if report:
        path = Path(str(report))
        if path.is_file() and path.suffix.lower() == ".csv":
            return path
    return None


def _load_manifest_qc(run: dict[str, Any]) -> dict[str, Any] | None:
    for art in run.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        raw = art.get("manifest_path") or ""
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            qc = data.get("qc")
            if isinstance(qc, dict):
                return {"airline": data.get("airline") or "", "qc": qc}
        except (OSError, json.JSONDecodeError):
            continue
    return None


def summarize_run_business(run: dict[str, Any]) -> dict[str, Any]:
    """Plain-language outcome for a single run (history + run detail)."""
    kind = _business_kind(run.get("script_id"))
    status = str(run.get("status") or "").lower()
    headline = ""
    detail_parts: list[str] = []
    stats: dict[str, Any] = {"kind": kind}

    airline = _param(run, "airlines", "airline", "airline_id")
    origin = _param(run, "origin", "sector_from")
    dest = _param(run, "destination", "sector_to")
    sector = f"{origin}–{dest}" if origin and dest else (origin or dest)

    if kind == "ocr":
        rows = _int_or_none(run.get("rows_total"))
        manifest = _load_manifest_qc(run)
        qc = (manifest or {}).get("qc") or {}
        if not airline and manifest:
            airline = str(manifest.get("airline") or "")
        csv_path = _resolve_csv_path(run)
        pob = {"adult": 0, "child": 0, "infant": 0}
        if csv_path:
            sums = _sum_csv_int(
                csv_path,
                [
                    "pob1_adult",
                    "pob1_child",
                    "pob1_infant",
                    "pob2_adult",
                    "pob2_child",
                    "pob2_infant",
                ],
            )
            pob["adult"] = sums["pob1_adult"] + sums["pob2_adult"]
            pob["child"] = sums["pob1_child"] + sums["pob2_child"]
            pob["infant"] = sums["pob1_infant"] + sums["pob2_infant"]
        flagged = _int_or_none(qc.get("rows_flagged")) or 0
        low_conf = _int_or_none(qc.get("rows_low_confidence")) or 0
        stats.update(
            {
                "rows": rows,
                "airline": airline,
                "pob_adult": pob["adult"],
                "pob_child": pob["child"],
                "pob_infant": pob["infant"],
                "qc_flagged": flagged,
                "qc_low_confidence": low_conf,
                "needs_check": flagged > 0
                or low_conf > 0
                or bool(qc.get("row_count_mismatch")),
            }
        )
        if status == "success":
            headline = "Passenger list ready"
            if rows is not None:
                detail_parts.append(f"{rows} flight row{'s' if rows != 1 else ''}")
            if airline:
                detail_parts.append(airline)
            pob_total = pob["adult"] + pob["child"] + pob["infant"]
            if pob_total:
                detail_parts.append(
                    f"POB {pob['adult']}A / {pob['child']}C / {pob['infant']}I"
                )
            if stats["needs_check"]:
                detail_parts.append("needs human check")
        elif status in {"failed", "cancelled", "timeout"}:
            headline = "Passenger extract failed"
            if airline:
                detail_parts.append(airline)

    elif kind == "pnr":
        booked = _int_or_none(run.get("booked"))
        failed = _int_or_none(run.get("failed"))
        csv_path = _resolve_csv_path(run)
        if csv_path and (booked is None or failed is None):
            counts = _count_pnr_csv(csv_path)
            if booked is None:
                booked = counts["booked"] or counts["with_pnr"] or None
            if failed is None:
                failed = counts["failed"] or None
        stats.update(
            {
                "booked": booked,
                "failed": failed,
                "sector": sector,
                "flight": _param(run, "flight_number"),
                "group": _param(run, "group_name"),
            }
        )
        if status == "success":
            headline = "Booking run finished"
            if booked is not None:
                detail_parts.append(f"{booked} booked")
            if failed:
                detail_parts.append(f"{failed} failed")
            if sector:
                detail_parts.append(sector)
            if stats["flight"]:
                detail_parts.append(str(stats["flight"]))
        elif status in {"failed", "cancelled", "timeout"}:
            headline = "Booking run failed"
            if sector:
                detail_parts.append(sector)

    elif kind == "api":
        rows = _int_or_none(run.get("rows_total"))
        mode = _param(run, "mode")
        agency = _param(run, "agency_id")
        stats.update({"rows": rows, "mode": mode, "agency": agency, "sector": sector})
        if status == "success":
            headline = "Booking extract ready"
            if rows is not None:
                detail_parts.append(f"{rows} record{'s' if rows != 1 else ''}")
            if mode:
                detail_parts.append(mode)
            if sector:
                detail_parts.append(sector)
            if agency:
                detail_parts.append(agency)
        elif status in {"failed", "cancelled", "timeout"}:
            headline = "Booking extract failed"
            if mode:
                detail_parts.append(mode)

    else:
        if status == "success":
            headline = "Run finished"
            rows = _int_or_none(run.get("rows_total"))
            if rows is not None:
                detail_parts.append(f"{rows} rows")
        elif status in {"failed", "cancelled", "timeout"}:
            headline = "Run needs attention"
        else:
            headline = _STATUS_LABELS.get(status, status.replace("_", " ").title())

    who = str(run.get("started_by") or "").strip()
    if who:
        detail_parts.append(f"by {who}")

    return {
        "kind": kind,
        "headline": headline,
        "line": " · ".join(p for p in detail_parts if p),
        "stats": stats,
        "airline": airline,
        "sector": sector,
        "reuse_href": _reuse_href(run),
        "download_href": _primary_download(run) if status == "success" else "",
        "detail_href": f"/history/{run['job_id']}" if run.get("job_id") else "",
    }


def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    from launcher.run_naming import attach_run_display_fields

    status = str(run.get("status") or "").lower()
    dt = _parse_run_dt(run)
    biz = summarize_run_business(run)
    item = attach_run_display_fields(
        {
            "job_id": run.get("job_id"),
            "script_id": run.get("script_id"),
            "script_name": run.get("script_name") or run.get("script_id") or "Job",
            "run_name": run.get("run_name")
            or (run.get("parameters") or {}).get("run_name"),
            "status": status,
            "status_label": _STATUS_LABELS.get(status, status.replace("_", " ").title()),
            "when_label": _relative_when(dt),
            "started_by": run.get("started_by") or "",
            "error_message": (run.get("error_message") or "")[:160],
            "headline": biz["headline"],
            "line": biz["line"],
            "reuse_href": biz["reuse_href"],
            "download_href": biz["download_href"],
            "detail_href": biz["detail_href"],
            "kind": biz["kind"],
            "needs_check": bool((biz.get("stats") or {}).get("needs_check")),
        }
    )
    return item


def _today_runs() -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    for run in _load_all_runs():
        day = _run_utc_date(run)
        if day is None:
            continue
        if day == today:
            rows.append(run)
        elif day < today:
            break
    return rows


def build_today_desk(
    *,
    scripts: list[dict[str, Any]] | None = None,
    pipelines: list[dict[str, Any]] | None = None,
    running_count: int = 0,
) -> dict[str, Any]:
    """Today desk: waiting + ready files (start new work from Workflows)."""
    _ = (scripts, pipelines)  # call-site still passes catalog; not used here
    today_runs = _today_runs()

    waiting_all = [
        r
        for r in today_runs
        if str(r.get("status") or "").lower() in {"failed", "cancelled", "timeout"}
    ]
    ready_all = [
        r
        for r in today_runs
        if str(r.get("status") or "").lower() == "success"
        and (r.get("report_path") or r.get("artifacts") or r.get("outputs"))
    ]

    return {
        "running_count": int(running_count or 0),
        "waiting": [_compact_run(r) for r in waiting_all[:_MAX_LIST]],
        "waiting_count": len(waiting_all),
        "ready": [_compact_run(r) for r in ready_all[:_MAX_LIST]],
        "ready_count": len(ready_all),
        "today_total": len(today_runs),
    }
