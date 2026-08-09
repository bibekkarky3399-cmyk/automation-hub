"""Background subprocess execution with queue, cancel, and timeout."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from launcher.config_loader import (
    PROJECT_ROOT,
    get_runtime_config,
    get_script_by_id,
    resolve_python_interpreter,
    resolve_result_ui,
)
from launcher.history import record_run
from launcher.notifications import notify_job_finished
from launcher.report_finder import find_report_for_job
from launcher.validators import coerce_bool


@dataclass
class JobState:
    job_id: str
    script_id: str
    script_name: str
    status: str  # queued | running | success | failed | cancelled | timeout
    created_at: str
    parameters: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    return_code: int | None = None
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    progress_label: str = "Queued…"
    error_message: str | None = None
    report_path: str | None = None
    output_dir: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    images_processed: int = 0
    queue_position: int | None = None
    started_by: str | None = None
    notify_on_complete: bool = False
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _cancel_requested: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append_stdout(self, line: str) -> None:
        with self._lock:
            self.stdout.append(line)

    def append_stderr(self, line: str) -> None:
        with self._lock:
            self.stderr.append(line)

    def snapshot(
        self,
        include_output: bool = True,
        since: int = 0,
        since_stderr: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            stdout_slice = self.stdout[since:] if include_output else []
            stderr_slice = self.stderr[since_stderr:] if include_output else []
            from launcher.run_naming import extract_run_name

            run_name = extract_run_name(self.parameters)
            return {
                "job_id": self.job_id,
                "script_id": self.script_id,
                "script_name": self.script_name,
                "run_name": run_name,
                "display_name": run_name or self.script_name,
                "workflow_name": self.script_name,
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "return_code": self.return_code,
                "progress_label": self.progress_label,
                "error_message": self.error_message,
                "report_path": self.report_path,
                "report_url": f"/report/{self.job_id}/view" if self.report_path else None,
                "report_is_csv": bool(
                    self.report_path and str(self.report_path).lower().endswith(".csv")
                ),
                "output_dir": self.output_dir,
                "outputs": self._snapshot_outputs(),
                "result_ui": resolve_result_ui(self.script_id),
                "summary": dict(self.summary),
                "images_processed": self.images_processed,
                "queue_position": self.queue_position,
                "stdout_line_count": len(self.stdout),
                "stderr_line_count": len(self.stderr),
                "stdout": stdout_slice,
                "stderr": stderr_slice,
                "duration_seconds": self._duration_seconds(),
                "started_by": self.started_by,
                "notify_on_complete": self.notify_on_complete,
                "parameters": dict(self.parameters or {}),
                "cancellable": self.status in {"queued", "running"},
            }

    def _snapshot_outputs(self) -> list[dict[str, Any]]:
        ui = resolve_result_ui(self.script_id)
        cached = (self.summary or {}).get("outputs")
        if cached and ui.get("mode") == "run_summary":
            return list(cached)[:1]
        artifacts = collect_run_artifacts(
            artifacts=(self.summary or {}).get("artifacts"),
            output_dir=self.output_dir,
            report_path=self.report_path,
            started_at=self.started_at,
            script_id=self.script_id,
        )
        if ui.get("mode") == "run_summary" and artifacts:
            artifacts = artifacts[:1]
        return build_job_outputs(
            self.job_id, artifacts=artifacts, report_path=self.report_path
        )

    def _duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        if self.finished_at:
            end = datetime.fromisoformat(self.finished_at)
        else:
            end = datetime.now(timezone.utc)
        return round((end - start).total_seconds(), 2)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._active = 0
        self._worker_started = False

    def create_job(
        self,
        script_id: str,
        parameters: dict[str, Any],
        *,
        started_by: str | None = None,
    ) -> JobState:
        script = get_script_by_id(script_id)
        if not script.get("enabled", True):
            raise ValueError("Disabled")

        job_id = str(uuid.uuid4())
        job = JobState(
            job_id=job_id,
            script_id=script_id,
            script_name=script["name"],
            status="queued",
            created_at=_utc_now(),
            parameters=dict(parameters),
            progress_label="Queued…",
            started_by=(started_by or "").strip() or None,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._refresh_queue_positions()
            if not self._worker_started:
                self._worker_started = True
                threading.Thread(target=self._queue_worker, daemon=True).start()

        return job

    def get_job(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def enable_background_notify(self, job_id: str) -> dict[str, Any]:
        """Mark a running/queued job to notify its starter when finished."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "error": "Job not found"}
            if job.status not in {"queued", "running"}:
                return {"ok": False, "error": f"Job is already {job.status}"}
            job.notify_on_complete = True
            return {
                "ok": True,
                "job_id": job.job_id,
                "notify_on_complete": True,
                "started_by": job.started_by,
                "status": job.status,
            }

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "error": "Job not found"}

            if job.status == "queued":
                if job_id in self._queue:
                    self._queue.remove(job_id)
                job.status = "cancelled"
                job.finished_at = _utc_now()
                job.progress_label = "Cancelled"
                job.error_message = "Cancelled while queued."
                self._refresh_queue_positions()
                self._finalize_side_effects(job)
                return {"ok": True, "status": "cancelled"}

            if job.status == "running":
                job._cancel_requested = True
                proc = job._proc
                if proc and proc.poll() is None:
                    proc.terminate()
                return {"ok": True, "status": "cancelling"}

            return {"ok": False, "error": f"Job is already {job.status}"}

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.snapshot(include_output=False) for j in self._jobs.values()]

    def list_active_jobs(self) -> list[dict[str, Any]]:
        """Queued or running jobs for the Active Jobs board."""
        with self._lock:
            jobs = [
                j for j in self._jobs.values() if j.status in {"queued", "running"}
            ]
            jobs.sort(key=lambda j: j.created_at or "", reverse=True)
            from launcher.secrets import redact_parameters

            items: list[dict[str, Any]] = []
            for j in jobs:
                snap = j.snapshot(include_output=False)
                snap["parameters"] = redact_parameters(snap.get("parameters") or {})
                items.append(
                    {
                        **snap,
                        "kind": "workflow",
                        "monitor_url": f"/runner/{j.script_id}?job={j.job_id}",
                    }
                )
            return items

    def run_inline(
        self,
        script_id: str,
        parameters: dict[str, Any],
        *,
        log_sink=None,
    ) -> JobState:
        """Run a script synchronously (used by pipelines; bypasses the queue)."""
        script = get_script_by_id(script_id)
        if not script.get("enabled", True):
            raise ValueError("Disabled")

        job_id = str(uuid.uuid4())
        job = JobState(
            job_id=job_id,
            script_id=script_id,
            script_name=script["name"],
            status="queued",
            created_at=_utc_now(),
            parameters=dict(parameters),
            progress_label="Starting…",
        )
        with self._lock:
            self._jobs[job_id] = job

        self._run_job(job, script, parameters, log_sink=log_sink)
        return job

    def _refresh_queue_positions(self) -> None:
        for idx, jid in enumerate(self._queue):
            job = self._jobs.get(jid)
            if job:
                job.queue_position = idx + 1

    def _queue_worker(self) -> None:
        while True:
            runtime = get_runtime_config()
            max_jobs = int(runtime.get("max_concurrent_jobs") or 1)
            job: JobState | None = None
            script: dict[str, Any] | None = None

            with self._lock:
                if self._active < max_jobs and self._queue:
                    job_id = self._queue.pop(0)
                    job = self._jobs.get(job_id)
                    self._refresh_queue_positions()
                    if job and not job._cancel_requested:
                        self._active += 1
                        try:
                            script = get_script_by_id(job.script_id)
                        except KeyError:
                            job.status = "failed"
                            job.error_message = "Script no longer configured"
                            job.finished_at = _utc_now()
                            self._active -= 1
                            job = None
                    elif job:
                        job.status = "cancelled"
                        job.finished_at = _utc_now()
                        job.error_message = "Cancelled while queued."
                        self._finalize_side_effects(job)
                        job = None

            if job and script is not None:
                threading.Thread(
                    target=self._run_job_guarded,
                    args=(job, script),
                    daemon=True,
                ).start()
            else:
                time.sleep(0.2)

    def _run_job_guarded(self, job: JobState, script: dict[str, Any]) -> None:
        try:
            self._run_job(job, script, job.parameters)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)

    def _run_job(
        self,
        job: JobState,
        script: dict[str, Any],
        parameters: dict[str, Any],
        *,
        log_sink=None,
    ) -> None:
        job.status = "running"
        job.started_at = _utc_now()
        job.progress_label = "Starting…"
        job.queue_position = None
        start_monotonic = time.monotonic()

        runtime = get_runtime_config()
        timeout = int(
            script.get("timeout_seconds")
            or runtime.get("default_timeout_seconds")
            or 1800
        )
        max_bytes = int(runtime.get("max_image_bytes") or (25 * 1024 * 1024))
        parameters = dict(parameters)
        # Inject runtime limit into CLI when OCR script supports it
        if "max_image_bytes" not in parameters:
            parameters["max_image_bytes"] = max_bytes

        try:
            cmd = _build_command(script, parameters)
        except ValueError as exc:
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = _utc_now()
            self._finalize_side_effects(job)
            return

        # OCR-only runtime limit (do not inject into other workflows)
        if script.get("id") == "ocr" and "--max-image-bytes" not in cmd:
            cmd.extend(["--max-image-bytes", str(max_bytes)])

        cwd = PROJECT_ROOT / script.get("cwd", ".")
        # Ensure fixed --output / report.search_dir folders exist before launch.
        _ensure_output_dirs(script, cwd)

        # Unbuffered child stdout so the Hub live log updates while the script runs.
        # Force UTF-8 on Windows so Unicode (e.g. →) is not misread as cp1252 (â†’).
        child_env = os.environ.copy()
        child_env["PYTHONUNBUFFERED"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env,
            )
        except PermissionError:
            job.status = "failed"
            job.error_message = "Permission denied when starting the script."
            job.finished_at = _utc_now()
            self._finalize_side_effects(job)
            return
        except OSError as exc:
            job.status = "failed"
            job.error_message = f"Could not start process: {exc}"
            job.finished_at = _utc_now()
            self._finalize_side_effects(job)
            return

        job._proc = proc
        assert proc.stdout is not None
        assert proc.stderr is not None

        def read_stream(stream, append_fn, is_err: bool) -> None:
            for line in stream:
                line = line.rstrip("\n")
                append_fn(line)
                if log_sink is not None:
                    try:
                        prefix = "[stderr] " if is_err else ""
                        log_sink(f"{prefix}{line}")
                    except Exception:
                        pass
                if not is_err:
                    _update_progress(job, script, line)
                    if line.startswith("Processing:"):
                        job.images_processed += 1

        t_out = threading.Thread(target=read_stream, args=(proc.stdout, job.append_stdout, False))
        t_err = threading.Thread(target=read_stream, args=(proc.stderr, job.append_stderr, True))
        t_out.start()
        t_err.start()

        timed_out = False
        while proc.poll() is None:
            if job._cancel_requested:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if time.monotonic() - start_monotonic > timeout:
                timed_out = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(0.25)

        t_out.join(timeout=2)
        t_err.join(timeout=2)

        job.return_code = proc.returncode
        job.finished_at = _utc_now()
        job.summary["duration_seconds"] = round(time.monotonic() - start_monotonic, 2)

        full_out = "\n".join(job.stdout)
        _extract_summary(job, script, full_out)

        if job._cancel_requested:
            job.status = "cancelled"
            job.progress_label = "Cancelled"
            job.error_message = "Cancelled by user."
            self._finalize_side_effects(job)
            return

        if timed_out:
            job.status = "timeout"
            job.progress_label = "Timed out"
            job.error_message = f"Exceeded timeout of {timeout}s."
            self._finalize_side_effects(job)
            return

        if proc.returncode != 0:
            job.status = "failed"
            job.error_message = _failure_message(job)
            self._finalize_side_effects(job)
            return

        report_path = find_report_for_job(script, full_out, job.started_at)
        if report_path:
            job.report_path = str(report_path.resolve())
            job.output_dir = str(report_path.parent.resolve())
        elif script.get("report"):
            job.status = "failed"
            job.error_message = "Script finished but no output file was found."
            self._finalize_side_effects(job)
            return

        job.progress_label = "Complete"
        job.status = "success"
        self._finalize_side_effects(job)

    def _finalize_side_effects(self, job: JobState) -> None:
        rows_total = None
        if "rows_total" in job.summary:
            try:
                rows_total = int(job.summary["rows_total"])
            except (TypeError, ValueError):
                rows_total = None

        artifacts = _parse_artifacts("\n".join(job.stdout))
        # Filesystem fallback covers filenames with spaces / partial stdout capture.
        output_hint = job.output_dir
        if not output_hint and job.report_path:
            output_hint = str(Path(job.report_path).parent)
        if not output_hint:
            try:
                script = get_script_by_id(job.script_id)
                search = (script.get("report") or {}).get("search_dir")
                if search:
                    output_hint = str((PROJECT_ROOT / search).resolve())
            except KeyError:
                pass
        if not output_hint:
            from launcher.config_loader import get_output_dir

            output_hint = str(get_output_dir())
        artifacts = _merge_artifacts(
            artifacts,
            _discover_artifacts_from_dir(output_hint, job.started_at),
        )
        # Prefer sum of artifact rows when available
        if artifacts and rows_total is None:
            rows_total = sum(int(a.get("rows") or 0) for a in artifacts)

        job.summary["artifacts"] = artifacts
        if rows_total is not None:
            job.summary["rows_total"] = rows_total

        result_ui = resolve_result_ui(job.script_id)
        job.summary["result_ui"] = result_ui

        # Prefer first artifact as the primary report when multiple CSVs exist.
        if artifacts:
            first_csv = artifacts[0].get("csv_path")
            if first_csv and Path(first_csv).is_file():
                job.report_path = str(Path(first_csv).resolve())
                job.output_dir = str(Path(first_csv).resolve().parent)
            # Only OCR-style runs treat artifact count as images processed.
            if not job.images_processed and result_ui.get("mode") == "per_source":
                job.images_processed = len(artifacts)

        # run_summary workflows surface one primary file (not a multi-image picker).
        primary_artifacts = artifacts
        if result_ui.get("mode") == "run_summary" and artifacts:
            primary_artifacts = [artifacts[0]]

        outputs = build_job_outputs(
            job.job_id, artifacts=primary_artifacts, report_path=job.report_path
        )
        job.summary["outputs"] = outputs
        job.summary["artifacts"] = primary_artifacts if result_ui.get("mode") == "run_summary" else artifacts

        from launcher.secrets import redact_parameters

        params = redact_parameters(job.parameters)
        job.summary["parameters"] = params

        from launcher.run_naming import extract_run_name

        run_name = extract_run_name(params)
        entry = {
            "job_id": job.job_id,
            "script_id": job.script_id,
            "script_name": job.script_name,
            "run_name": run_name,
            "status": job.status,
            "started_by": job.started_by,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "duration_seconds": job.summary.get("duration_seconds") or job._duration_seconds(),
            "images_processed": job.images_processed,
            "rows_total": rows_total,
            "qc_summary": job.summary.get("qc_summary"),
            "booked": job.summary.get("booked"),
            "posted": job.summary.get("posted"),
            "failed": job.summary.get("failed"),
            "result_ui": result_ui,
            "report_path": job.report_path,
            "output_dir": job.output_dir,
            "error_message": job.error_message,
            "parameters": params,
            "artifacts": job.summary.get("artifacts") or artifacts,
            "outputs": outputs,
            "notify_on_complete": bool(job.notify_on_complete),
        }
        try:
            record_run(entry)
        except OSError:
            pass
        notify_job_finished(entry)
        from launcher.user_notifications import notify_starter_job_finished

        notify_starter_job_finished(entry)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_command(script: dict[str, Any], parameters: dict[str, Any]) -> list[str]:
    python = resolve_python_interpreter()
    script_path = PROJECT_ROOT / script["script"]
    if not script_path.is_file():
        raise ValueError(f"Script file not found: {script_path}")

    cmd = [str(python), str(script_path)]
    mode = str(
        parameters.get("csv_mode") or parameters.get("input_mode") or "folder"
    ).lower()
    folder_ids = {"image_folder", "csv_folder"}
    file_ids = {"image_file", "csv"}

    for inp in script.get("inputs", []):
        inp_id = inp["id"]
        cli = inp.get("cli") or {}
        value = parameters.get(inp_id)

        # Mode-aware required inputs (OCR images / API CSV)
        if inp_id in folder_ids and mode not in {"folder", "all"}:
            continue
        if inp_id in file_ids and mode != "file":
            continue
        if inp_id == "csv_list" and mode != "list":
            continue

        if inp.get("type") == "boolean":
            flag = cli.get("flag")
            if not flag:
                continue
            # Support argparse.BooleanOptionalAction: --flag / --no-flag
            # Coerce so string "false" / missing values don't become True.
            if value is None:
                value = inp.get("default", False)
            enabled = coerce_bool(value, default=bool(inp.get("default", False)))
            if enabled:
                cmd.append(flag)
            elif flag.startswith("--") and not flag.startswith("--no-"):
                cmd.append(f"--no-{flag[2:]}")
            continue

        # Hidden fields are not rendered in the form; use scripts.json defaults.
        if inp.get("hidden") and value in (None, "") and inp.get("default") not in (None, ""):
            value = inp.get("default")

        required = bool(inp.get("required")) and not bool(inp.get("hidden"))
        if inp_id in folder_ids and mode in {"folder", "all"}:
            required = True
        if inp_id in file_ids and mode == "file":
            required = True
        if inp_id == "csv_list" and mode == "list":
            required = True

        if required and not value:
            raise ValueError(f"Missing required input: {inp.get('label', inp_id)}")

        if value in (None, ""):
            continue

        flag = cli.get("flag")
        if flag:
            cmd.extend([flag, str(value)])
        elif cli.get("position") is not None:
            cmd.append(str(value))

    # Config-only args — not collected from the form.
    # Strip any legacy per-script --output; shared folder comes from scripts.json output_dir.
    fixed = list(script.get("fixed_args") or [])
    skip_next = False
    for arg in fixed:
        if skip_next:
            skip_next = False
            continue
        if arg in {"--output", "-o"}:
            skip_next = True
            continue
        cmd.append(str(arg))

    from launcher.config_loader import get_output_dir

    out_dir = get_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd.extend(["--output", str(out_dir)])

    return cmd


def _ensure_output_dirs(script: dict[str, Any], cwd: Path) -> None:
    """Create the shared output folder (and any leftover per-script paths)."""
    from launcher.config_loader import get_output_dir

    candidates: list[Path] = [get_output_dir()]
    fixed = list(script.get("fixed_args") or [])
    for i, arg in enumerate(fixed):
        if arg in {"--output", "-o"} and i + 1 < len(fixed):
            candidates.append(Path(fixed[i + 1]))
    search_dir = (script.get("report") or {}).get("search_dir")
    if search_dir:
        candidates.append(Path(search_dir))
    for path in candidates:
        resolved = path if path.is_absolute() else (cwd / path)
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def _update_progress(job: JobState, script: dict[str, Any], line: str) -> None:
    for hint in script.get("progress_hints", []):
        if hint.get("match") and hint["match"] in line:
            job.progress_label = hint.get("label", job.progress_label)
            break


def _extract_summary(job: JobState, script: dict[str, Any], full_out: str) -> None:
    patterns = script.get("summary_patterns") or {}
    for key, pattern in patterns.items():
        m = re.search(pattern, full_out)
        if m:
            job.summary[key] = m.group(1).strip() if m.lastindex else m.group(0)
    job.summary["images_processed"] = job.images_processed
    if "report_path" in job.summary:
        job.output_dir = str(Path(job.summary["report_path"]).parent)


def _failure_message(job: JobState) -> str:
    if job.stderr:
        return job.stderr[-1]
    for line in reversed(job.stdout):
        if "error" in line.lower():
            return line
    return f"Script exited with code {job.return_code}"


def _parse_artifact_fields(line: str) -> dict[str, str]:
    """Parse key="value with spaces" or key=token fields from an ARTIFACT line."""
    fields: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|(\S+))', line):
        key = match.group(1)
        fields[key] = match.group(2) if match.group(2) is not None else (match.group(3) or "")
    return fields


def _parse_artifacts(stdout_text: str) -> list[dict[str, Any]]:
    """Parse ARTIFACT: source=… csv=… manifest=… rows=… lines from OCR stdout."""
    artifacts: list[dict[str, Any]] = []
    for line in stdout_text.splitlines():
        if "ARTIFACT:" not in line:
            continue
        fields = _parse_artifact_fields(line.split("ARTIFACT:", 1)[1])
        csv_path = fields.get("csv") or ""
        if not csv_path:
            continue
        rows_raw = fields.get("rows")
        artifacts.append(
            {
                "source_image": fields.get("source") or "",
                "source_path": fields.get("source_path") or "",
                "csv_path": csv_path,
                "csv_name": Path(csv_path).name,
                "manifest_path": fields.get("manifest") or "",
                "rows": int(rows_raw) if rows_raw and rows_raw.isdigit() else None,
            }
        )
    return artifacts


def _discover_artifacts_from_dir(
    output_dir: str | Path | None,
    started_at_iso: str | None,
) -> list[dict[str, Any]]:
    """Build artifact list from *.manifest.json written during the job window."""
    if not output_dir:
        return []
    root = Path(output_dir)
    if not root.is_dir():
        return []

    started: datetime | None = None
    if started_at_iso:
        try:
            started = datetime.fromisoformat(started_at_iso)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except ValueError:
            started = None

    found: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*.manifest.json"), key=lambda p: p.stat().st_mtime):
        try:
            mtime = datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        # Keep files created during/after the job (small skew allowance).
        if started and mtime < started - timedelta(seconds=5):
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        csv_path = data.get("csv_path") or str(manifest_path.with_suffix(".csv"))
        csv_file = Path(csv_path)
        if not csv_file.is_file():
            alt = manifest_path.with_suffix(".csv")
            if alt.is_file():
                csv_file = alt
            else:
                continue
        qc = data.get("qc") or {}
        rows = qc.get("rows_total")
        found.append(
            {
                "source_image": data.get("source_image") or csv_file.name,
                "source_path": "",
                "csv_path": str(csv_file.resolve()),
                "csv_name": csv_file.name,
                "manifest_path": str(manifest_path.resolve()),
                "rows": int(rows) if rows is not None else None,
            }
        )
    return found


def _merge_artifacts(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union artifacts by resolved csv path; keep first-seen order (stdout then disk)."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for art in list(primary or []) + list(secondary or []):
        path_str = str(art.get("csv_path") or "").strip()
        if not path_str:
            continue
        try:
            key = str(Path(path_str).resolve())
        except OSError:
            key = path_str
        if key in seen:
            continue
        seen.add(key)
        item = dict(art)
        item["csv_path"] = key
        item["csv_name"] = item.get("csv_name") or Path(key).name
        merged.append(item)
    return merged


def collect_run_artifacts(
    *,
    artifacts: list[dict[str, Any]] | None = None,
    output_dir: str | None = None,
    report_path: str | None = None,
    started_at: str | None = None,
    script_id: str | None = None,
) -> list[dict[str, Any]]:
    """Best-effort artifact list for UI (stdout + manifests on disk)."""
    hint = output_dir
    if not hint and report_path:
        try:
            hint = str(Path(report_path).parent)
        except OSError:
            hint = None
    if not hint and script_id:
        try:
            script = get_script_by_id(script_id)
            search = (script.get("report") or {}).get("search_dir")
            if search:
                hint = str((PROJECT_ROOT / search).resolve())
        except KeyError:
            pass
    if not hint:
        from launcher.config_loader import get_output_dir

        hint = str(get_output_dir())
    return _merge_artifacts(
        list(artifacts or []),
        _discover_artifacts_from_dir(hint, started_at),
    )


def build_job_outputs(
    job_id: str,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    report_path: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize run outputs for a generic multi-file result UI.

    OCR batch → one entry per image/CSV. Single-file workflows → one entry.
    """
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()

    for art in artifacts or []:
        path_str = str(art.get("csv_path") or "").strip()
        if not path_str:
            continue
        try:
            path = Path(path_str).resolve()
        except OSError:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        index = len(outputs)
        filename = art.get("csv_name") or path.name
        label = art.get("source_image") or filename
        kind = "csv" if path.suffix.lower() == ".csv" else "file"
        outputs.append(
            {
                "id": str(index),
                "index": index,
                "label": label,
                "kind": kind,
                "path": key,
                "filename": filename,
                "rows": art.get("rows"),
                "source_image": art.get("source_image") or "",
                "view_url": f"/report/{job_id}/file/{index}/view",
                "download_url": f"/report/{job_id}/file/{index}/download",
            }
        )

    if not outputs and report_path:
        try:
            path = Path(report_path).resolve()
        except OSError:
            path = None
        if path and path.is_file():
            kind = "csv" if path.suffix.lower() == ".csv" else "file"
            outputs.append(
                {
                    "id": "0",
                    "index": 0,
                    "label": path.name,
                    "kind": kind,
                    "path": str(path),
                    "filename": path.name,
                    "rows": None,
                    "source_image": "",
                    "view_url": f"/report/{job_id}/file/0/view",
                    "download_url": f"/report/{job_id}/file/0/download",
                }
            )
    return outputs


job_manager = JobManager()
