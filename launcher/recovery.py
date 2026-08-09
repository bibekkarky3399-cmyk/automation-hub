"""Plain-language recovery hints for failed runs."""

from __future__ import annotations

from typing import Any


def failure_tips(message: str | None, *, status: str | None = None, stage: str | None = None) -> list[str]:
    """Return short, actionable tips based on error text / status."""
    text = f"{message or ''} {stage or ''}".lower()
    status_key = (status or "").strip().lower()
    tips: list[str] = []

    def add(tip: str) -> None:
        if tip not in tips:
            tips.append(tip)

    if status_key == "cancelled":
        add("This run was stopped early. Start it again when you are ready.")
    elif status_key == "timeout":
        add("The job hit its time limit. Try a smaller batch, or ask an admin to raise the timeout.")
    elif status_key == "failed":
        add("Review the settings below, fix the issue, then use Run again.")

    if any(k in text for k in ("login", "password", "credential", "auth", "unauthorized", "401", "403")):
        add("Portal login failed — check username and password, then try again.")
    if any(k in text for k in ("no seat", "sold out", "unavailable", "no flight", "not found", "no availability")):
        add("Flight or seats may be unavailable for those dates — confirm sector, flight number, and dates.")
    if any(k in text for k in ("isoformat", "invalid date", "date")):
        add("Check the travel dates — use a valid from/to date range.")
    if any(k in text for k in ("timeout", "timed out", "took too long")):
        add("Portal or network was slow — retry, or narrow the date range / fewer images.")
    if any(k in text for k in ("no image", "empty folder", "folder", "does not exist", "no files")):
        add("Confirm the photo folder exists and contains flight-log images.")
    if any(k in text for k in ("connection", "endpoint", "refused", "network", "dns", "unreachable", "ssl")):
        add("Cannot reach the booking service — check network and the API endpoint.")
    if any(k in text for k in ("permission", "denied", "read-only", "writable")):
        add("Check folder permissions for reading inputs and writing outputs.")
    if any(k in text for k in ("playwright", "browser", "chromium")):
        add("Browser booking needs Chromium — ask an admin to check Playwright.")
    if any(k in text for k in ("qc", "confidence", "mismatch")):
        add("Quality check found issues — open the CSV and review flagged rows before sending on.")
    if any(k in text for k in ("csv", "parse", "column", "schema")):
        add("Open the output (if any) and confirm the file format matches expectations.")

    if not tips:
        add("Open the log details, fix the cause, then Run again with the same settings.")
        add("If it keeps failing, copy the job reference and share it with your admin.")

    return tips[:4]


def enrich_run_recovery(run: dict[str, Any]) -> dict[str, Any]:
    """Attach recovery tips to a run dict (mutates and returns)."""
    status = str(run.get("status") or "")
    if status in {"failed", "cancelled", "timeout"}:
        run["recovery_tips"] = failure_tips(
            run.get("error_message"),
            status=status,
            stage=None,
        )
    return run
