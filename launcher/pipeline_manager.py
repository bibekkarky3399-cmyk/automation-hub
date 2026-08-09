"""Configurable multi-step workflow chains (pipelines) from scripts.json."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from launcher.config_loader import get_pipeline_by_id, get_script_by_id, resolve_result_ui
from launcher.history import record_run
from launcher.job_manager import build_job_outputs, job_manager
from launcher.pipeline_compose import build_step_parameters


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PipelineStepState:
    id: str
    script_id: str
    label: str
    status: str = "pending"  # pending | running | success | failed | skipped
    job_id: str | None = None
    error_message: str | None = None
    report_path: str | None = None
    output_dir: str | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "script_id": self.script_id,
            "label": self.label,
            "status": self.status,
            "job_id": self.job_id,
            "error_message": self.error_message,
            "report_path": self.report_path,
            "output_dir": self.output_dir,
            "outputs": list(self.outputs),
        }


@dataclass
class PipelineRun:
    run_id: str
    pipeline_id: str
    pipeline_name: str
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    steps: list[PipelineStepState] = field(default_factory=list)
    progress_label: str = "Queued…"
    error_message: str | None = None
    report_path: str | None = None
    output_dir: str | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    result_ui: dict[str, Any] = field(default_factory=dict)
    stdout: list[str] = field(default_factory=list)
    started_by: str | None = None
    notify_on_complete: bool = False
    _cancel_requested: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append_log(self, line: str) -> None:
        with self._lock:
            self.stdout.append(line)

    def snapshot(self, *, since: int = 0) -> dict[str, Any]:
        with self._lock:
            active_idx = next(
                (i for i, s in enumerate(self.steps) if s.status == "running"),
                None,
            )
            if active_idx is None:
                done = sum(1 for s in self.steps if s.status in {"success", "failed", "skipped"})
                active_idx = min(done, max(0, len(self.steps) - 1))

            from launcher.run_naming import extract_run_name

            run_name = extract_run_name(self.parameters)
            return {
                "job_id": self.run_id,
                "run_id": self.run_id,
                "pipeline_id": self.pipeline_id,
                "script_id": f"pipeline:{self.pipeline_id}",
                "script_name": self.pipeline_name,
                "run_name": run_name,
                "display_name": run_name or self.pipeline_name,
                "workflow_name": self.pipeline_name,
                "is_pipeline": True,
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "progress_label": self.progress_label,
                "error_message": self.error_message,
                "report_path": self.report_path,
                "report_url": f"/report/{self.run_id}/view" if self.report_path else None,
                "report_is_csv": bool(
                    self.report_path and str(self.report_path).lower().endswith(".csv")
                ),
                "output_dir": self.output_dir,
                "outputs": list(self.outputs),
                "result_ui": dict(self.result_ui)
                or resolve_result_ui(f"pipeline:{self.pipeline_id}"),
                "steps": [s.as_dict() for s in self.steps],
                "active_step_index": active_idx,
                "stdout": self.stdout[since:],
                "stderr": [],
                "stdout_line_count": len(self.stdout),
                "stderr_line_count": 0,
                "cancellable": self.status in {"queued", "running"},
                "duration_seconds": self._duration_seconds(),
                "started_by": self.started_by,
                "notify_on_complete": self.notify_on_complete,
                "parameters": dict(self.parameters or {}),
                "summary": {
                    "steps_total": len(self.steps),
                    "steps_ok": sum(1 for s in self.steps if s.status == "success"),
                    "steps_failed": sum(1 for s in self.steps if s.status == "failed"),
                    **dict(self.summary),
                },
            }

    def _duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = (
            datetime.fromisoformat(self.finished_at)
            if self.finished_at
            else datetime.now(timezone.utc)
        )
        return round((end - start).total_seconds(), 2)


def resolve_bind(
    bind: dict[str, Any] | None,
    *,
    inputs: dict[str, Any],
    step_results: dict[str, PipelineStepState],
) -> dict[str, Any]:
    """Resolve step.bind into concrete script parameters."""
    params: dict[str, Any] = {}
    for target, spec in (bind or {}).items():
        if isinstance(spec, dict) and "const" in spec:
            params[target] = spec["const"]
            continue
        path = str(spec or "").strip()
        if not path:
            continue
        if path.startswith("inputs."):
            key = path[len("inputs.") :]
            params[target] = inputs.get(key)
        elif path.startswith("steps."):
            # steps.<step_id>.<field>
            parts = path.split(".")
            if len(parts) >= 3:
                step_id = parts[1]
                field = parts[2]
                step = step_results.get(step_id)
                if step is None:
                    raise ValueError(f"Bind references unknown step '{step_id}'")
                if field == "output_dir":
                    params[target] = step.output_dir
                elif field == "report_path":
                    params[target] = step.report_path
                elif field == "job_id":
                    params[target] = step.job_id
                elif field == "status":
                    params[target] = step.status
                elif field == "csv_paths":
                    # Newline-joined paths from this step's CSV outputs (pipeline chaining).
                    paths = [
                        str(o.get("path"))
                        for o in (step.outputs or [])
                        if o.get("path")
                    ]
                    if not paths:
                        raise ValueError(
                            f"Step '{step_id}' produced no CSV outputs to bind"
                        )
                    params[target] = "\n".join(paths)
                else:
                    raise ValueError(f"Unsupported step field in bind: {path}")
            else:
                raise ValueError(f"Invalid steps bind path: {path}")
        else:
            # Bare input id fallback
            params[target] = inputs.get(path, path)
    return params


class PipelineManager:
    def __init__(self) -> None:
        self._runs: dict[str, PipelineRun] = {}
        self._lock = threading.Lock()

    def get_run(self, run_id: str) -> PipelineRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_active_runs(self) -> list[dict[str, Any]]:
        """Queued or running pipelines for the Active Jobs board."""
        with self._lock:
            runs = [
                r for r in self._runs.values() if r.status in {"queued", "running"}
            ]
            runs.sort(key=lambda r: r.created_at or "", reverse=True)
            from launcher.secrets import redact_parameters

            items: list[dict[str, Any]] = []
            for r in runs:
                snap = r.snapshot(since=len(r.stdout))  # omit log bodies for list view
                snap.pop("stdout", None)
                snap.pop("stderr", None)
                snap["parameters"] = redact_parameters(snap.get("parameters") or {})
                items.append(
                    {
                        **snap,
                        "kind": "pipeline",
                        "monitor_url": f"/pipeline/{r.pipeline_id}?job={r.run_id}",
                    }
                )
            return items

    def create_run(
        self,
        pipeline_id: str,
        parameters: dict[str, Any],
        *,
        started_by: str | None = None,
    ) -> PipelineRun:
        pipeline = get_pipeline_by_id(pipeline_id)
        # annotate_pipeline (via get_pipeline_by_id) folds in disabled step scripts.
        if not pipeline.get("enabled", True):
            raise ValueError("Disabled")

        steps_cfg = pipeline.get("steps") or []
        if len(steps_cfg) < 1:
            raise ValueError("Pipeline has no steps")

        # Validate referenced scripts exist up front
        for step in steps_cfg:
            get_script_by_id(step["script_id"])

        run_id = str(uuid.uuid4())
        run = PipelineRun(
            run_id=run_id,
            pipeline_id=pipeline_id,
            pipeline_name=pipeline["name"],
            status="queued",
            created_at=_utc_now(),
            parameters=dict(parameters),
            progress_label="Queued…",
            started_by=(started_by or "").strip() or None,
            steps=[
                PipelineStepState(
                    id=s["id"],
                    script_id=s["script_id"],
                    label=s.get("label") or get_script_by_id(s["script_id"]).get("name", s["script_id"]),
                )
                for s in steps_cfg
            ],
        )
        with self._lock:
            self._runs[run_id] = run

        threading.Thread(target=self._execute, args=(run, pipeline), daemon=True).start()
        return run

    def enable_background_notify(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            return {"ok": False, "error": "Pipeline run not found"}
        if run.status not in {"queued", "running"}:
            return {"ok": False, "error": f"Pipeline is already {run.status}"}
        run.notify_on_complete = True
        return {
            "ok": True,
            "job_id": run.run_id,
            "notify_on_complete": True,
            "started_by": run.started_by,
            "status": run.status,
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            return {"ok": False, "error": "Pipeline run not found"}
        if run.status not in {"queued", "running"}:
            return {"ok": False, "error": f"Pipeline is already {run.status}"}
        run._cancel_requested = True
        # Best-effort cancel active child job
        for step in run.steps:
            if step.status == "running" and step.job_id:
                job_manager.cancel_job(step.job_id)
        run.append_log("Cancel requested…")
        return {"ok": True, "status": "cancelling"}

    def _execute(self, run: PipelineRun, pipeline: dict[str, Any]) -> None:
        run.status = "running"
        run.started_at = _utc_now()
        run.progress_label = "Starting pipeline…"
        run.append_log(f"Pipeline: {run.pipeline_name} ({run.pipeline_id})")
        run.append_log(f"Steps: {len(run.steps)}")

        steps_cfg = {s["id"]: s for s in (pipeline.get("steps") or [])}
        step_results: dict[str, PipelineStepState] = {}

        try:
            for step in run.steps:
                if run._cancel_requested:
                    step.status = "skipped"
                    run.status = "cancelled"
                    run.error_message = "Cancelled by user."
                    run.progress_label = "Cancelled"
                    break

                cfg = steps_cfg[step.id]
                run.progress_label = f"Running: {step.label}"
                run.append_log("")
                run.append_log(f"── Step [{step.id}] {step.label} ({step.script_id}) ──")
                step.status = "running"

                try:
                    params = build_step_parameters(
                        cfg,
                        inputs=run.parameters,
                        step_results=step_results,
                        resolve_bind_fn=resolve_bind,
                    )
                except ValueError as exc:
                    step.status = "failed"
                    step.error_message = str(exc)
                    run.status = "failed"
                    run.error_message = f"Step '{step.id}' bind error: {exc}"
                    run.append_log(f"❌ {run.error_message}")
                    break

                from launcher.secrets import is_sensitive_key

                run.append_log(
                    "Params: "
                    + ", ".join(
                        f"{k}={'***' if is_sensitive_key(k) else v}"
                        for k, v in params.items()
                        if v not in (None, "")
                    )
                )

                job = job_manager.run_inline(
                    step.script_id,
                    params,
                    log_sink=run.append_log,
                )
                step.job_id = job.job_id
                step.report_path = job.report_path
                step.output_dir = job.output_dir
                step.outputs = build_job_outputs(
                    job.job_id,
                    artifacts=(job.summary or {}).get("artifacts"),
                    report_path=job.report_path,
                )
                step_results[step.id] = step

                if job.status == "success":
                    step.status = "success"
                    run.append_log(f"✅ Step '{step.id}' succeeded")
                    # Primary deliverable = last successful step (not OCR intermediates).
                    run.report_path = job.report_path
                    run.output_dir = job.output_dir
                    run.outputs = list(step.outputs)
                    run.result_ui = resolve_result_ui(step.script_id)
                    job_summary = job.summary or {}
                    run.summary = {
                        k: job_summary.get(k)
                        for k in (
                            "booked",
                            "posted",
                            "failed",
                            "rows_total",
                            "qc_summary",
                        )
                        if job_summary.get(k) is not None
                    }
                else:
                    step.status = "failed"
                    step.error_message = job.error_message or f"Script status: {job.status}"
                    run.status = "failed"
                    run.error_message = f"Step '{step.id}' failed: {step.error_message}"
                    run.append_log(f"❌ {run.error_message}")
                    # Skip remaining
                    for later in run.steps:
                        if later.status == "pending":
                            later.status = "skipped"
                    break
            else:
                # Completed all steps without break
                if run.status == "running":
                    run.status = "success"
                    run.progress_label = "Complete"
                    run.append_log("Pipeline complete.")

        except Exception as exc:
            run.status = "failed"
            run.error_message = str(exc)
            run.progress_label = "Failed"
            run.append_log(f"❌ Pipeline error: {exc}")

        run.finished_at = _utc_now()
        if run.status == "success":
            run.progress_label = "Complete"
        elif run.status == "cancelled":
            run.progress_label = "Cancelled"
        elif run.status == "failed":
            run.progress_label = "Failed"

        self._record_history(run)

    def _record_history(self, run: PipelineRun) -> None:
        from launcher.secrets import redact_parameters

        params = redact_parameters(run.parameters)
        # Keep step outputs for audit; primary UI outputs = final successful step only.
        primary_step = next(
            (s for s in reversed(run.steps) if s.status == "success" and s.outputs),
            None,
        )
        artifacts = []
        if primary_step:
            for out in primary_step.outputs:
                artifacts.append(
                    {
                        "source_image": out.get("label") or out.get("filename"),
                        "csv_path": out.get("path"),
                        "csv_name": out.get("filename"),
                        "rows": out.get("rows"),
                        "step_id": primary_step.id,
                    }
                )
        result_ui = run.result_ui or resolve_result_ui(f"pipeline:{run.pipeline_id}")
        if result_ui.get("mode") == "run_summary" and artifacts:
            artifacts = artifacts[:1]
        outputs = build_job_outputs(
            run.run_id, artifacts=artifacts, report_path=run.report_path
        )
        from launcher.run_naming import extract_run_name

        run_name = extract_run_name(params)
        entry = {
            "job_id": run.run_id,
            "script_id": f"pipeline:{run.pipeline_id}",
            "script_name": run.pipeline_name,
            "run_name": run_name,
            "status": run.status,
            "started_by": run.started_by,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": run._duration_seconds(),
            "images_processed": 0,
            "rows_total": run.summary.get("rows_total"),
            "booked": run.summary.get("booked"),
            "posted": run.summary.get("posted"),
            "failed": run.summary.get("failed"),
            "qc_summary": run.summary.get("qc_summary"),
            "result_ui": result_ui,
            "report_path": run.report_path,
            "output_dir": run.output_dir,
            "error_message": run.error_message,
            "parameters": params,
            "artifacts": artifacts,
            "outputs": outputs,
            "pipeline": {
                "pipeline_id": run.pipeline_id,
                "steps": [s.as_dict() for s in run.steps],
            },
            "notify_on_complete": bool(run.notify_on_complete),
        }
        try:
            record_run(entry)
        except OSError:
            pass
        from launcher.notifications import notify_job_finished
        from launcher.user_notifications import notify_starter_job_finished

        notify_job_finished(entry)
        notify_starter_job_finished(entry)


pipeline_manager = PipelineManager()
