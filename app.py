#!/usr/bin/env python3
"""
Web launcher for existing Python automation scripts.
Does not implement OCR or report logic — only runs configured scripts and displays output.
"""

from __future__ import annotations

import csv
import io
import os
import platform
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from launcher.auth import (
    authenticate,
    is_admin_role,
    normalize_role,
    user_can_cancel_job,
)
from launcher.config_loader import (
    CONFIG_PATH,
    PROJECT_ROOT,
    ConfigError,
    create_script_stub,
    delete_pipeline,
    delete_script,
    get_deleted_workflow,
    get_pipeline_by_id,
    get_runtime_config,
    get_script_by_id,
    get_scripts_config_raw,
    list_deleted_workflows,
    list_pipelines_public,
    list_scripts_public,
    load_scripts_config,
    patch_pipeline,
    patch_script,
    purge_deleted_workflow,
    reload_scripts_config,
    resolve_result_ui,
    restore_deleted_workflow,
    upsert_pipeline,
)
from launcher.history import (
    build_home_ops,
    build_runs_query,
    build_day_graph,
    build_outcome_pie,
    build_workflow_pie,
    compute_metrics,
    compute_metrics_by_workflow,
    decorate_runs_for_display,
    get_run,
    latest_run_summaries_by_script,
    list_runs,
    query_runs,
    resolve_runs_date_window,
)
from launcher.job_manager import build_job_outputs, collect_run_artifacts, job_manager
from launcher.pipeline_manager import pipeline_manager
from launcher.recovery import enrich_run_recovery
from launcher.schema_validator import validate_scripts_config
from launcher.secrets import redact_parameters
from launcher.run_naming import extract_run_name, with_run_name_input
from launcher.today_desk import build_today_desk, summarize_run_business
from launcher.user_notifications import (
    ack_user_notifications,
    list_user_notifications,
)
from launcher.version import APP_VERSION
from launcher.validators import (
    coerce_bool,
    ensure_uploads_dir,
    is_path_under_project,
    validate_file_path,
    validate_folder,
    validate_report_path,
    validate_select,
    validate_text,
)

def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from .env into os.environ (no extra dependency)."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # default; raised from runtime on upload
# Stable secret so sessions survive reloads; override in real deployments.
app.config["SECRET_KEY"] = os.environ.get(
    "HUB_SECRET_KEY", "automation-hub-dev-secret-change-me"
)
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 12  # 12 hours

# Endpoints that do not require a session.
_PUBLIC_ENDPOINTS = frozenset(
    {
        "login",
        "logout",
        "api_health",
        "static",
    }
)

# Settings + config mutation — admin only.
_ADMIN_ENDPOINTS = frozenset(
    {
        "settings_page",
        "api_settings_config",
        "api_settings_patch_script",
        "api_settings_create_script",
        "api_settings_delete_script",
        "api_settings_deleted_list",
        "api_settings_deleted_detail",
        "api_settings_deleted_restore",
        "api_settings_deleted_purge",
        "api_settings_patch_pipeline",
        "api_settings_create_pipeline",
        "api_settings_delete_pipeline",
        "api_settings_reload",
        "api_settings_backup",
    }
)


def _is_logged_in() -> bool:
    return bool(session.get("user"))


def _current_role() -> str:
    return normalize_role(session.get("role"))


def _current_user() -> str:
    return str(session.get("user") or "")


def _is_admin() -> bool:
    return is_admin_role(_current_role())


def _safe_next_url(candidate: str | None) -> str:
    """Only allow same-origin relative redirects after login."""
    if not candidate:
        return url_for("index")
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for("index")
    if not candidate.startswith("/"):
        return url_for("index")
    return candidate


def _forbid_for_role(message: str = "Access denied") -> Any:
    if (
        request.path.startswith("/api/")
        or request.path.startswith("/status/")
        or request.path.startswith("/output/")
        or request.path.startswith("/run-script")
        or request.path.startswith("/run-pipeline")
    ):
        return jsonify({"error": message}), 403
    abort(403)


@app.context_processor
def _inject_auth():
    logged_in = _is_logged_in()
    role = _current_role() if logged_in else ""
    return {
        "current_user": _current_user() if logged_in else "",
        "current_role": role,
        "is_admin": _is_admin() if logged_in else False,
        "app_version": APP_VERSION,
    }


@app.before_request
def _apply_upload_limit():
    try:
        runtime = get_runtime_config()
        app.config["MAX_CONTENT_LENGTH"] = int(
            runtime.get("max_image_bytes") or (25 * 1024 * 1024)
        ) + (1024 * 1024)
    except Exception:
        pass


@app.before_request
def _require_login():
    endpoint = request.endpoint or ""
    if endpoint in _PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return None
    if not _is_logged_in():
        if (
            request.path.startswith("/api/")
            or request.path.startswith("/status/")
            or request.path.startswith("/output/")
            or request.path.startswith("/run-script")
            or request.path.startswith("/run-pipeline")
        ):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login", next=request.path))

    # Legacy sessions without role → operator.
    if not session.get("role"):
        session["role"] = "operator"

    if endpoint in _ADMIN_ENDPOINTS and not _is_admin():
        return _forbid_for_role("Admin access required")
    if request.path.startswith("/settings") and not _is_admin():
        return _forbid_for_role("Admin access required")
    if request.path.startswith("/api/settings") and not _is_admin():
        return _forbid_for_role("Admin access required")
    return None


@app.errorhandler(403)
def _forbidden(_exc):
    if (
        request.path.startswith("/api/")
        or request.accept_mimetypes.best == "application/json"
    ):
        return jsonify({"error": "Admin access required"}), 403
    return (
        render_template(
            "forbidden.html",
            current_user=_current_user(),
            current_role=_current_role(),
        ),
        403,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if _is_logged_in():
        return redirect(_safe_next_url(request.args.get("next") or request.form.get("next")))

    error = None
    username = ""
    next_url = request.args.get("next") or request.form.get("next") or "/"

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        account = authenticate(username, password)
        if account:
            session.clear()
            session["user"] = account["username"]
            session["role"] = account["role"]
            session.permanent = True
            return redirect(_safe_next_url(next_url))
        error = "Invalid username or password."

    return render_template("login.html", error=error, username=username, next=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    scripts = list_scripts_public()
    pipelines = list_pipelines_public()
    running_count = len(job_manager.list_active_jobs()) + len(
        pipeline_manager.list_active_runs()
    )
    ops = build_home_ops(current_user=_current_user(), running_count=running_count)
    desk = build_today_desk(
        scripts=scripts,
        pipelines=pipelines,
        running_count=running_count,
    )
    return render_template("index.html", ops=ops, desk=desk)


@app.route("/workflows")
def workflows_page():
    scripts = list_scripts_public()
    pipelines = list_pipelines_public()
    latest = latest_run_summaries_by_script()
    for s in scripts:
        s["last_run"] = latest.get(s["id"])
    for p in pipelines:
        p["last_run"] = latest.get(f"pipeline:{p['id']}")
    return render_template(
        "workflows.html", scripts=scripts, pipelines=pipelines
    )


@app.route("/metrics")
def metrics_page():
    scripts = list_scripts_public()
    script_id = request.args.get("workflow") or request.args.get("script_id") or ""
    by_wf = compute_metrics_by_workflow(scripts)
    selected = None
    if script_id:
        selected = next((w for w in by_wf["workflows"] if w["script_id"] == script_id), None)
        if not selected:
            abort(404)
        metrics = selected["metrics"]
        workflow_pie = None
    else:
        metrics = by_wf["overall"]
        workflow_pie = build_workflow_pie(by_wf["workflows"])
    day_graph = build_day_graph(metrics.get("runs_by_day_series") or [])
    return render_template(
        "metrics.html",
        metrics=metrics,
        workflows=by_wf["workflows"],
        outcome_pie=build_outcome_pie(metrics),
        workflow_pie=workflow_pie,
        day_graph=day_graph,
        selected_script_id=script_id or None,
        selected_workflow=selected,
        scripts=scripts,
    )


@app.route("/history")
def history_page():
    scripts = list_scripts_public()
    pipelines = list_pipelines_public()
    script_id = request.args.get("workflow") or request.args.get("script_id") or ""
    status_filter = (request.args.get("status") or "").strip().lower()
    search_q = (request.args.get("q") or "").strip()
    range_key = (request.args.get("range") or "").strip().lower()
    date_from_raw = (request.args.get("from") or "").strip()
    date_to_raw = (request.args.get("to") or "").strip()
    page = request.args.get("page", 1, type=int) or 1

    if script_id:
        known = {s["id"] for s in scripts} | {f"pipeline:{p['id']}" for p in pipelines}
        from launcher.history import list_script_ids_in_history

        if script_id not in known and script_id not in list_script_ids_in_history():
            abort(404)

    preset, day_from, day_to = resolve_runs_date_window(
        range_key=range_key,
        date_from=date_from_raw,
        date_to=date_to_raw,
    )
    result = query_runs(
        script_id=script_id or None,
        status=status_filter or None,
        date_from=day_from,
        date_to=day_to,
        q=search_q or None,
        page=page,
        page_size=50,
    )

    selected_name = None
    if script_id:
        if script_id.startswith("pipeline:"):
            try:
                selected_name = get_pipeline_by_id(
                    script_id.split(":", 1)[1], compose_inputs=False
                ).get("name")
            except KeyError:
                selected_name = script_id
        else:
            try:
                selected_name = get_script_by_id(script_id).get("name")
            except KeyError:
                selected_name = script_id

    def runs_qs(**overrides):
        # Templates may pass date_from/date_to (avoids Python/Jinja `from` keyword).
        if "date_from" in overrides:
            overrides["from"] = overrides.pop("date_from")
        if "date_to" in overrides:
            overrides["to"] = overrides.pop("date_to")
        base = {
            "workflow": script_id or None,
            "status": status_filter or None,
            "q": search_q or None,
            "range": None if preset == "custom" else preset,
            "from": day_from.isoformat() if preset == "custom" and day_from else None,
            "to": day_to.isoformat() if preset == "custom" and day_to else None,
            "page": None,
        }
        base.update(overrides)
        if base.get("page") in (1, "1", None):
            base["page"] = None
        # Default view is today — omit range=today for cleaner URLs.
        if base.get("range") == "today":
            base["range"] = None
        q = build_runs_query(**base)
        return f"/history?{q}" if q else "/history"

    return render_template(
        "runs.html",
        runs=decorate_runs_for_display(result["runs"]),
        scripts=scripts,
        pipelines=pipelines,
        selected_script_id=script_id or None,
        selected_name=selected_name,
        status_filter=status_filter or None,
        search_q=search_q,
        date_preset=preset,
        date_from=day_from.isoformat() if day_from else "",
        date_to=day_to.isoformat() if day_to else "",
        page=result["page"],
        pages=result["pages"],
        page_size=result["page_size"],
        total_runs=result["total"],
        has_prev=result["has_prev"],
        has_next=result["has_next"],
        runs_qs=runs_qs,
    )


@app.route("/jobs")
def active_jobs_page():
    return render_template("jobs.html")


@app.get("/api/jobs/active")
def api_jobs_active():
    jobs = job_manager.list_active_jobs() + pipeline_manager.list_active_runs()
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    actor = _current_user()
    for job in jobs:
        job["cancellable"] = bool(job.get("cancellable")) and user_can_cancel_job(
            actor, job.get("started_by")
        )
        job["is_mine"] = user_can_cancel_job(actor, job.get("started_by"))
    return jsonify(
        {
            "ok": True,
            "count": len(jobs),
            "jobs": jobs,
            "current_user": actor,
        }
    )


@app.route("/runs")
def runs_page_redirect():
    qs = request.query_string.decode("utf-8")
    return redirect(f"/history?{qs}" if qs else "/history", code=301)


@app.route("/runs/<job_id>")
def run_detail_redirect(job_id: str):
    return redirect(f"/history/{job_id}", code=301)


@app.route("/history/<job_id>")
def run_detail_page(job_id: str):
    run = get_run(job_id)
    if not run:
        # Fall back to in-memory job if still available
        job = job_manager.get_job(job_id)
        pipe = pipeline_manager.get_run(job_id) if not job else None
        if job:
            run = job.snapshot(include_output=False)
            run["parameters"] = redact_parameters(job.parameters)
            run["artifacts"] = (job.summary or {}).get("artifacts") or []
            run["script_id"] = job.script_id
        elif pipe:
            run = pipe.snapshot()
            run["parameters"] = redact_parameters(pipe.parameters)
        else:
            abort(404)
    else:
        # Re-redact in case older history stored plaintext secrets.
        run["parameters"] = redact_parameters(run.get("parameters") or {})
    result_ui = run.get("result_ui") or resolve_result_ui(run.get("script_id"))
    run["result_ui"] = result_ui

    # Prefer stored primary outputs for run_summary / pipelines — do not re-expand
    # intermediate OCR CSVs into an image-name picker.
    stored_outputs = run.get("outputs") or []
    if result_ui.get("mode") == "run_summary" and stored_outputs:
        if len(stored_outputs) > 1:
            run["outputs"] = stored_outputs[:1]
    else:
        artifacts = collect_run_artifacts(
            artifacts=run.get("artifacts"),
            output_dir=run.get("output_dir"),
            report_path=run.get("report_path"),
            started_at=run.get("started_at"),
            script_id=run.get("script_id"),
        )
        if result_ui.get("mode") == "run_summary" and artifacts:
            artifacts = artifacts[:1]
        run["artifacts"] = artifacts
        run["outputs"] = build_job_outputs(
            job_id,
            artifacts=artifacts,
            report_path=run.get("report_path"),
        )
    enrich_run_recovery(run)
    display = decorate_runs_for_display([run])[0]
    display["recovery_tips"] = run.get("recovery_tips") or []
    # Recompute business summary after outputs/artifacts are attached
    display["business"] = summarize_run_business(display)
    display["outcome_line"] = display["business"].get("line") or ""
    display["outcome_headline"] = display["business"].get("headline") or ""
    return render_template("run_detail.html", run=display)


@app.get("/api/health")
def api_health():
    try:
        scripts = list_scripts_public()
        cfg_errors = validate_scripts_config(load_scripts_config())
        return jsonify(
            {
                "status": "ok" if not cfg_errors else "degraded",
                "version": APP_VERSION,
                "scripts": len(scripts),
                "config_errors": cfg_errors,
                "runtime": get_runtime_config(),
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503


@app.get("/api/metrics")
def api_metrics():
    script_id = request.args.get("script_id") or request.args.get("workflow")
    if script_id:
        return jsonify(compute_metrics(script_id=script_id))
    return jsonify(compute_metrics_by_workflow(list_scripts_public()))


@app.get("/api/runs")
def api_runs():
    limit = request.args.get("limit", 50, type=int)
    script_id = request.args.get("script_id") or request.args.get("workflow")
    return jsonify(
        {
            "script_id": script_id,
            "runs": list_runs(limit=min(limit, 200), script_id=script_id or None),
        }
    )


@app.get("/api/runs/<job_id>")
def api_run_detail(job_id: str):
    run = get_run(job_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    payload = dict(run)
    payload["parameters"] = redact_parameters(payload.get("parameters") or {})
    return jsonify(payload)


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel_job(job_id: str):
    job = job_manager.get_job(job_id)
    pipe = pipeline_manager.get_run(job_id) if not job else None
    if not job and not pipe:
        return jsonify({"error": "Job not found"}), 404
    started_by = (job.started_by if job else pipe.started_by) if (job or pipe) else None
    if not user_can_cancel_job(_current_user(), started_by):
        return jsonify(
            {
                "error": "Only the user who started this job can stop it.",
                "started_by": started_by,
            }
        ), 403
    if pipe:
        result = pipeline_manager.cancel_run(job_id)
    else:
        result = job_manager.cancel_job(job_id)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.post("/api/jobs/<job_id>/background")
def api_job_background(job_id: str):
    """Detach UI: keep job running and notify the starter when it finishes."""
    job = job_manager.get_job(job_id)
    pipe = pipeline_manager.get_run(job_id) if not job else None
    if not job and not pipe:
        return jsonify({"error": "Job not found"}), 404
    started_by = job.started_by if job else pipe.started_by
    if not user_can_cancel_job(_current_user(), started_by):
        return jsonify(
            {
                "error": "Only the user who started this job can run it in background with notifications.",
                "started_by": started_by,
            }
        ), 403
    if job:
        result = job_manager.enable_background_notify(job_id)
    else:
        result = pipeline_manager.enable_background_notify(job_id)
    if not result.get("ok"):
        return jsonify(result), 400
    result["monitor_url"] = (
        f"/runner/{job.script_id}?job={job_id}"
        if job
        else f"/pipeline/{pipe.pipeline_id}?job={job_id}"
    )
    result["started_by"] = started_by
    return jsonify(result)


@app.get("/api/notifications")
def api_notifications():
    unread_only = (request.args.get("unread") or "").lower() in {"1", "true", "yes"}
    items = list_user_notifications(_current_user(), unread_only=unread_only)
    return jsonify(
        {
            "ok": True,
            "notifications": items,
            "unread": sum(1 for i in items if not i.get("read")),
        }
    )


@app.post("/api/notifications/ack")
def api_notifications_ack():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if ids is not None and not isinstance(ids, list):
        return jsonify({"error": "ids must be a list"}), 400
    marked = ack_user_notifications(_current_user(), ids)
    return jsonify({"ok": True, "marked": marked})


@app.route("/settings")
def settings_page():
    return render_template(
        "settings.html",
        config_path=str(CONFIG_PATH.relative_to(PROJECT_ROOT)),
    )


@app.route("/api/settings/config", methods=["GET"])
def api_settings_config():
    try:
        config = get_scripts_config_raw()
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 500
    from launcher.config_loader import get_output_dir

    return jsonify(
        {
            "ok": True,
            "path": str(CONFIG_PATH.relative_to(PROJECT_ROOT)),
            "schema_version": config.get("schema_version"),
            "output_dir": config.get("output_dir") or "outputs",
            "output_dir_resolved": str(get_output_dir()),
            "scripts": config.get("scripts") or [],
            "pipelines": config.get("pipelines") or [],
        }
    )


@app.route("/api/settings/scripts/<script_id>", methods=["PATCH"])
def api_settings_patch_script(script_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        patch_script(script_id, payload)
        return jsonify({"ok": True, "scripts": list_scripts_public()})
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/scripts", methods=["POST"])
def api_settings_create_script():
    payload = request.get_json(silent=True) or {}
    script_id = str(payload.get("id") or "").strip()
    name = str(payload.get("name") or script_id).strip()
    description = str(payload.get("description") or "").strip()
    try:
        create_script_stub(script_id, name, description)
        return jsonify({"ok": True, "scripts": list_scripts_public()}), 201
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/scripts/<script_id>", methods=["DELETE"])
def api_settings_delete_script(script_id: str):
    try:
        result = delete_script(script_id)
        return jsonify(
            {
                "ok": True,
                "deleted_files": result.get("deleted_files") or [],
                "backup": result.get("backup"),
                "scripts": list_scripts_public(),
                "pipelines": list_pipelines_public(),
                "deleted": list_deleted_workflows(),
            }
        )
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/deleted", methods=["GET"])
def api_settings_deleted_list():
    return jsonify({"ok": True, "deleted": list_deleted_workflows()})


@app.route("/api/settings/deleted/<backup_id>", methods=["GET"])
def api_settings_deleted_detail(backup_id: str):
    try:
        return jsonify({"ok": True, **get_deleted_workflow(backup_id)})
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 404


@app.route("/api/settings/deleted/<backup_id>/restore", methods=["POST"])
def api_settings_deleted_restore(backup_id: str):
    try:
        result = restore_deleted_workflow(backup_id)
        return jsonify(
            {
                "ok": True,
                **result,
                "scripts": list_scripts_public(),
                "deleted": list_deleted_workflows(),
            }
        )
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/deleted/<backup_id>", methods=["DELETE"])
def api_settings_deleted_purge(backup_id: str):
    try:
        result = purge_deleted_workflow(backup_id)
        return jsonify(
            {
                "ok": True,
                **result,
                "deleted": list_deleted_workflows(),
            }
        )
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/pipelines/<pipeline_id>", methods=["PATCH"])
def api_settings_patch_pipeline(pipeline_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        patch_pipeline(pipeline_id, payload)
        return jsonify({"ok": True, "pipelines": list_pipelines_public()})
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/pipelines", methods=["POST"])
def api_settings_create_pipeline():
    payload = request.get_json(silent=True) or {}
    try:
        upsert_pipeline(
            {
                "id": payload.get("id"),
                "name": payload.get("name") or payload.get("id"),
                "description": payload.get("description") or "",
                "icon": payload.get("icon") or "bi-diagram-3",
                "badge": payload.get("badge") or "Pipeline",
                "enabled": bool(payload.get("enabled", True)),
                "timeout_seconds": int(payload.get("timeout_seconds") or 3600),
                "form_note": payload.get("form_note") or "",
                "steps": payload.get("steps") or [],
            },
            create=True,
        )
        return jsonify({"ok": True, "pipelines": list_pipelines_public()}), 201
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/pipelines/<pipeline_id>", methods=["DELETE"])
def api_settings_delete_pipeline(pipeline_id: str):
    try:
        delete_pipeline(pipeline_id)
        return jsonify({"ok": True, "pipelines": list_pipelines_public()})
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/settings/reload", methods=["POST"])
def api_settings_reload():
    try:
        reload_scripts_config()
        return jsonify(
            {
                "ok": True,
                "scripts": list_scripts_public(),
                "pipelines": list_pipelines_public(),
            }
        )
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/settings/backup")
def api_settings_backup():
    """Download a zip of history, outputs, config, scripts, and related project files."""
    from flask import Response

    from launcher.backup import build_backup_zip

    try:
        payload, filename = build_backup_zip()
    except OSError as exc:
        return jsonify({"error": f"Could not build backup: {exc}"}), 500
    return Response(
        payload,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


@app.route("/runner/<script_id>")
def runner_page(script_id: str):
    try:
        script = get_script_by_id(script_id)
    except KeyError:
        abort(404)
    return render_template(
        "runner.html",
        script_id=script_id,
        script=script,
        project_root=str(PROJECT_ROOT),
    )


@app.route("/pipeline/<pipeline_id>")
def pipeline_runner_page(pipeline_id: str):
    try:
        pipeline = get_pipeline_by_id(pipeline_id)
    except KeyError:
        abort(404)
    return render_template(
        "pipeline_runner.html",
        pipeline_id=pipeline_id,
        pipeline=pipeline,
        project_root=str(PROJECT_ROOT),
    )


@app.route("/report/<job_id>")
def report_page(job_id: str):
    job = job_manager.get_job(job_id)
    if not job or not job.report_path:
        abort(404)
    return render_template(
        "report.html",
        job_id=job_id,
        script_name=job.script_name,
        report_url=f"/report/{job_id}/view",
    )


# --- API ---


@app.get("/api/scripts")
def api_scripts():
    return jsonify({"scripts": list_scripts_public()})


@app.get("/api/pipelines")
def api_pipelines():
    return jsonify({"pipelines": list_pipelines_public()})


@app.get("/api/pipelines/<pipeline_id>")
def api_pipeline_detail(pipeline_id: str):
    try:
        pipeline = get_pipeline_by_id(pipeline_id)
    except KeyError:
        return jsonify({"error": "Pipeline not found"}), 404
    return jsonify(
        {
            "id": pipeline["id"],
            "name": pipeline["name"],
            "description": pipeline.get("description", ""),
            "icon": pipeline.get("icon", "bi-diagram-3"),
            "badge": pipeline.get("badge", "Pipeline"),
            "enabled": bool(pipeline.get("enabled", True)),
            "config_enabled": bool(pipeline.get("config_enabled", True)),
            "disabled_reason": pipeline.get("disabled_reason"),
            "blocked_steps": pipeline.get("blocked_steps") or [],
            "inputs": with_run_name_input(
                pipeline.get("inputs", []), workflow_name=pipeline.get("name") or ""
            ),
            "form_note": pipeline.get("form_note") or "",
            "steps": pipeline.get("steps") or [],
            "result_ui": resolve_result_ui(f"pipeline:{pipeline_id}"),
        }
    )


@app.get("/api/scripts/<script_id>")
def api_script_detail(script_id: str):
    try:
        script = get_script_by_id(script_id)
    except KeyError:
        return jsonify({"error": "Script not found"}), 404
    return jsonify(
        {
            "id": script["id"],
            "name": script["name"],
            "description": script.get("description", ""),
            "icon": script.get("icon", "bi-terminal"),
            "badge": script.get("badge", ""),
            "enabled": bool(script.get("enabled", True)),
            "inputs": with_run_name_input(
                script.get("inputs", []), workflow_name=script.get("name") or ""
            ),
            "form_note": script.get("form_note") or "",
            "stages": script.get("stages") or [],
            "summary_keys": script.get("summary_keys") or [],
            "progress_hints": script.get("progress_hints") or [],
            "result_ui": resolve_result_ui(script_id),
        }
    )


@app.post("/api/validate-folder")
def api_validate_folder():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    require_images = bool(data.get("require_images", False))
    result = validate_folder(path, require_images=require_images)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.get("/api/browse-folders")
def api_browse_folders():
    """List subfolders under the project root for the folder picker modal."""
    raw = request.args.get("path", "")
    base = PROJECT_ROOT.resolve()
    target = base if not raw else Path(raw).expanduser().resolve()

    if not is_path_under_project(target):
        return jsonify({"error": "Path is outside the project directory."}), 403
    if not target.is_dir():
        return jsonify({"error": "Not a directory."}), 400

    try:
        entries = sorted(
            [
                {
                    "name": p.name,
                    "path": str(p.resolve()),
                    "has_children": any(c.is_dir() for c in p.iterdir()) if p.is_dir() else False,
                }
                for p in target.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ],
            key=lambda e: e["name"].lower(),
        )
    except PermissionError:
        return jsonify({"error": "Permission denied reading this folder."}), 403

    parent = str(target.parent) if target != base else None
    if parent and not is_path_under_project(Path(parent)):
        parent = None

    return jsonify(
        {
            "current": str(target),
            "parent": parent,
            "root": str(base),
            "entries": entries,
        }
    )


@app.post("/api/upload-file")
def api_upload_file():
    """Stage an uploaded file under project/uploads/staged for script inputs."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "Empty filename."}), 400

    accept = request.form.get("accept", "")
    if accept:
        extensions = {
            e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
            for e in accept.split(",")
            if e.strip()
        }
        suffix = Path(upload.filename).suffix.lower()
        if extensions and suffix not in extensions:
            allowed = ", ".join(sorted(extensions))
            return jsonify({"error": f"File type not allowed. Accepted: {allowed}"}), 400

    uploads_dir = ensure_uploads_dir()
    safe_name = Path(upload.filename).name
    dest = uploads_dir / f"{uuid.uuid4().hex}_{safe_name}"
    try:
        upload.save(dest)
    except OSError as exc:
        return jsonify({"error": f"Could not save file: {exc}"}), 500

    return jsonify({"ok": True, "path": str(dest.resolve()), "filename": safe_name})



def _validate_form_parameters(inputs: list[dict], parameters: dict) -> tuple[dict | None, tuple | None]:
    """Validate/normalize form parameters. Returns (params, None) or (None, (jsonify, status))."""
    parameters = dict(parameters or {})
    mode = str(
        parameters.get("csv_mode") or parameters.get("input_mode") or "folder"
    ).lower()
    folder_ids = {"image_folder", "csv_folder"}
    file_ids = {"image_file", "csv"}
    runtime = get_runtime_config()
    max_bytes = int(runtime.get("max_image_bytes") or (25 * 1024 * 1024))
    inputs = with_run_name_input(inputs or [])

    for inp in inputs or []:
        inp_id = inp["id"]
        inp_type = inp.get("type", "text")
        value = parameters.get(inp_id)
        label = inp.get("label", inp_id)

        # Hidden fields are Settings-only; always fill from scripts.json defaults.
        if inp.get("hidden") and value in (None, "") and inp.get("default") not in (None, ""):
            value = inp.get("default")
            parameters[inp_id] = value

        if inp_id in folder_ids and mode != "folder":
            parameters[inp_id] = ""
            continue
        if inp_id in file_ids and mode != "file":
            parameters[inp_id] = ""
            continue

        required = bool(inp.get("required")) and not bool(inp.get("hidden"))
        if inp_id in folder_ids and mode == "folder":
            required = True
        if inp_id in file_ids and mode == "file":
            required = True

        if inp_type == "folder":
            if required and not value:
                return None, (jsonify({"error": f"{label} is required."}), 400)
            if value:
                require_images = bool(inp.get("require_images"))
                create_if_missing = bool(inp.get("create_if_missing"))
                check = validate_folder(
                    value,
                    require_images=require_images,
                    create_if_missing=create_if_missing,
                )
                if not check.get("ok"):
                    return None, (jsonify({"error": check.get("error", "Invalid folder")}), 400)
                parameters[inp_id] = check["path"]

        elif inp_type == "file":
            check = validate_file_path(
                value or "",
                accept=inp.get("accept"),
                required=required,
            )
            if not check.get("ok"):
                return None, (jsonify({"error": check.get("error", "Invalid file")}), 400)
            path = check.get("path") or ""
            if path and inp_id in {"image_file"}:
                size = Path(path).stat().st_size
                if size > max_bytes:
                    return None, (
                        jsonify(
                            {
                                "error": (
                                    f"Image exceeds max size "
                                    f"({size} bytes > {max_bytes} bytes limit)."
                                )
                            }
                        ),
                        400,
                    )
            parameters[inp_id] = path

        elif inp_type == "select":
            check = validate_select(
                value or "",
                inp.get("options", []),
                required=required or bool(inp.get("required")),
            )
            if not check.get("ok"):
                return None, (jsonify({"error": check.get("error", "Invalid selection")}), 400)
            parameters[inp_id] = check.get("value", "")

        elif inp_type == "text":
            check = validate_text(
                value or "",
                required=required,
                pattern=inp.get("pattern"),
            )
            if not check.get("ok"):
                return None, (jsonify({"error": check.get("error", "Invalid value")}), 400)
            parameters[inp_id] = check.get("value", "")

        elif inp_type == "boolean":
            if value is None:
                value = inp.get("default", False)
            parameters[inp_id] = coerce_bool(value, default=bool(inp.get("default", False)))

    # Hub-only label for History / Active jobs (never sent to scripts).
    run_label = extract_run_name(parameters)
    if not run_label:
        return None, (jsonify({"error": "Run name is required."}), 400)
    parameters["run_name"] = run_label

    return parameters, None


@app.post("/run-script")
def run_script():
    data = request.get_json(silent=True) or {}
    script_id = data.get("script_id")
    parameters = data.get("parameters") or {}

    if not script_id:
        return jsonify({"error": "script_id is required"}), 400

    try:
        script = get_script_by_id(script_id)
    except KeyError:
        return jsonify({"error": "Unknown script"}), 404

    if not script.get("enabled", True):
        return jsonify({"error": "Disabled"}), 403

    parameters, err = _validate_form_parameters(script.get("inputs") or [], parameters)
    if err:
        body, status = err
        return body, status

    try:
        job = job_manager.create_job(
            script_id, parameters, started_by=_current_user()
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(
        {
            "job_id": job.job_id,
            "status": job.status,
            "queue_position": job.queue_position,
            "started_by": job.started_by,
        }
    ), 202


@app.post("/run-pipeline")
def run_pipeline():
    data = request.get_json(silent=True) or {}
    pipeline_id = data.get("pipeline_id")
    parameters = data.get("parameters") or {}

    if not pipeline_id:
        return jsonify({"error": "pipeline_id is required"}), 400

    try:
        pipeline = get_pipeline_by_id(pipeline_id)
    except KeyError:
        return jsonify({"error": "Unknown pipeline"}), 404

    if not pipeline.get("enabled", True):
        return jsonify(
            {
                "error": "Disabled",
                "disabled_reason": "Disabled",
                "blocked_steps": pipeline.get("blocked_steps") or [],
            }
        ), 403

    parameters, err = _validate_form_parameters(pipeline.get("inputs") or [], parameters)
    if err:
        body, status = err
        return body, status

    try:
        run = pipeline_manager.create_run(
            pipeline_id, parameters, started_by=_current_user()
        )
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"job_id": run.run_id, "status": run.status, "is_pipeline": True}), 202


@app.get("/status/<job_id>")
def job_status(job_id: str):
    actor = _current_user()
    pipe = pipeline_manager.get_run(job_id)
    if pipe:
        since = request.args.get("since", 0, type=int)
        snap = pipe.snapshot(since=max(0, since))
        snap["parameters"] = redact_parameters(snap.get("parameters") or {})
        snap["cancellable"] = bool(snap.get("cancellable")) and user_can_cancel_job(
            actor, snap.get("started_by")
        )
        return jsonify(snap)
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    since = request.args.get("since", 0, type=int)
    since_stderr = request.args.get("since_stderr", 0, type=int)
    snap = job.snapshot(
        include_output=True, since=max(0, since), since_stderr=max(0, since_stderr)
    )
    snap["parameters"] = redact_parameters(snap.get("parameters") or {})
    snap["cancellable"] = bool(snap.get("cancellable")) and user_can_cancel_job(
        actor, snap.get("started_by")
    )
    return jsonify(snap)


@app.get("/output/<job_id>")
def job_output(job_id: str):
    pipe = pipeline_manager.get_run(job_id)
    if pipe:
        since = request.args.get("since", 0, type=int)
        with pipe._lock:
            return jsonify(
                {
                    "stdout": pipe.stdout[since:],
                    "stderr": [],
                    "stdout_total": len(pipe.stdout),
                    "stderr_total": 0,
                }
            )
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    since = request.args.get("since", 0, type=int)
    with job._lock:
        return jsonify(
            {
                "stdout": job.stdout[since:],
                "stderr": job.stderr[since:],
                "stdout_total": len(job.stdout),
                "stderr_total": len(job.stderr),
            }
        )


def _job_outputs_for(job_id: str) -> list[dict]:
    """Outputs from live job snapshot or persisted history (+ manifest fallback)."""
    pipe = pipeline_manager.get_run(job_id)
    if pipe and pipe.outputs:
        outs = list(pipe.outputs)
        ui = pipe.result_ui or resolve_result_ui(f"pipeline:{pipe.pipeline_id}")
        return outs[:1] if ui.get("mode") == "run_summary" else outs
    job = job_manager.get_job(job_id)
    if job:
        ui = resolve_result_ui(job.script_id)
        artifacts = collect_run_artifacts(
            artifacts=(job.summary or {}).get("artifacts"),
            output_dir=job.output_dir,
            report_path=job.report_path,
            started_at=job.started_at,
            script_id=job.script_id,
        )
        if ui.get("mode") == "run_summary" and artifacts:
            artifacts = artifacts[:1]
        outputs = build_job_outputs(
            job_id, artifacts=artifacts, report_path=job.report_path
        )
        if outputs:
            return outputs
    run = get_run(job_id)
    if run:
        ui = run.get("result_ui") or resolve_result_ui(run.get("script_id"))
        stored = run.get("outputs") or []
        if ui.get("mode") == "run_summary" and stored:
            return stored[:1]
        artifacts = collect_run_artifacts(
            artifacts=run.get("artifacts"),
            output_dir=run.get("output_dir"),
            report_path=run.get("report_path"),
            started_at=run.get("started_at"),
            script_id=run.get("script_id"),
        )
        if ui.get("mode") == "run_summary" and artifacts:
            artifacts = artifacts[:1]
        return build_job_outputs(
            job_id, artifacts=artifacts, report_path=run.get("report_path")
        )
    return []


def _resolve_report_path(job_id: str) -> tuple[Path, str]:
    """Return (path, script_name) from live job or history (primary / first output)."""
    outputs = _job_outputs_for(job_id)
    if outputs:
        path = Path(outputs[0]["path"])
        if validate_report_path(path):
            job = job_manager.get_job(job_id)
            if job:
                return path, job.script_name or job.script_id
            run = get_run(job_id)
            return path, (run or {}).get("script_name") or (run or {}).get("script_id") or "Report"

    pipe = pipeline_manager.get_run(job_id)
    if pipe and pipe.report_path:
        path = Path(pipe.report_path)
        if validate_report_path(path):
            return path, pipe.pipeline_name
    job = job_manager.get_job(job_id)
    if job and job.report_path:
        path = Path(job.report_path)
        if validate_report_path(path):
            return path, job.script_name or job.script_id
    run = get_run(job_id)
    if run and run.get("report_path"):
        path = Path(run["report_path"])
        if validate_report_path(path):
            return path, run.get("script_name") or run.get("script_id") or "Report"
    abort(404)


def _resolve_output_path(job_id: str, index: int) -> tuple[Path, dict]:
    outputs = _job_outputs_for(job_id)
    if index < 0 or index >= len(outputs):
        abort(404)
    meta = outputs[index]
    path = Path(meta["path"])
    if not validate_report_path(path):
        abort(404)
    return path, meta


def _parse_csv_for_preview(raw: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Parse CSV text, skipping blank/# metadata lines used by OCR exports.

    Returns (headers, data_rows, metadata_lines).
    """
    reader = csv.reader(io.StringIO(raw))
    metadata: list[str] = []
    content: list[list[str]] = []
    for row in reader:
        if not row or all(not str(c).strip() for c in row):
            continue
        first = str(row[0]).lstrip()
        # OCR contract writes "# key=value" sidecar lines before the header.
        if first.startswith("#"):
            metadata.append(" ".join(str(c).strip() for c in row if str(c).strip()))
            continue
        content.append([str(c) for c in row])

    if not content:
        return [], [], metadata

    # Prefer a wide header-like first row; fall back to max width across rows.
    headers = content[0]
    data_rows = content[1:]
    width = max(len(headers), max((len(r) for r in data_rows), default=0))
    if len(headers) < width:
        headers = headers + [f"col_{i+1}" for i in range(len(headers), width)]

    normalized: list[list[str]] = []
    for row in data_rows:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        normalized.append(row)
    return headers, normalized, metadata


def _render_csv_preview(
    path: Path,
    job_id: str,
    *,
    max_rows: int = 500,
    download_url: str | None = None,
    title: str | None = None,
):
    """Render CSV as an HTML table for in-browser preview."""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        abort(404)

    headers, normalized, metadata = _parse_csv_for_preview(raw)
    truncated = len(normalized) > max_rows
    shown = normalized[:max_rows]
    embedded = request.args.get("embed") in {"1", "true", "yes"}
    if download_url is None:
        download_url = url_for("report_download", job_id=job_id)
    return render_template(
        "csv_preview.html",
        filename=title or path.name,
        headers=headers,
        rows=shown,
        row_count=len(normalized),
        col_count=len(headers),
        truncated=truncated,
        shown_rows=len(shown),
        metadata=metadata,
        embedded=embedded,
        download_url=None if embedded else download_url,
    )


@app.get("/report/<job_id>/view")
def report_view(job_id: str):
    path, _script_name = _resolve_report_path(job_id)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _render_csv_preview(path, job_id)
    if suffix in {".html", ".htm"}:
        return send_file(path, mimetype="text/html")
    return send_file(path)


@app.get("/report/<job_id>/download")
def report_download(job_id: str):
    path, _script_name = _resolve_report_path(job_id)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.get("/report/<job_id>/file/<int:index>/view")
def report_file_view(job_id: str, index: int):
    path, meta = _resolve_output_path(job_id, index)
    suffix = path.suffix.lower()
    title = meta.get("label") or path.name
    download_url = url_for("report_file_download", job_id=job_id, index=index)
    if suffix == ".csv":
        return _render_csv_preview(
            path, job_id, download_url=download_url, title=title
        )
    if suffix in {".html", ".htm"}:
        return send_file(path, mimetype="text/html")
    return send_file(path)


@app.get("/report/<job_id>/file/<int:index>/download")
def report_file_download(job_id: str, index: int):
    path, meta = _resolve_output_path(job_id, index)
    name = meta.get("filename") or path.name
    return send_file(path, as_attachment=True, download_name=name)


@app.post("/api/open-output/<job_id>")
def open_output_folder(job_id: str):
    """Open report folder in the system file manager (local dev convenience)."""
    folder = None
    pipe = pipeline_manager.get_run(job_id)
    if pipe:
        folder = pipe.output_dir or (
            Path(pipe.report_path).parent if pipe.report_path else None
        )
    job = job_manager.get_job(job_id)
    if not folder and job:
        folder = job.output_dir or (Path(job.report_path).parent if job.report_path else None)
    if not folder:
        run = get_run(job_id)
        if run:
            folder = run.get("output_dir") or (
                Path(run["report_path"]).parent if run.get("report_path") else None
            )
    if not folder:
        return jsonify({"error": "No output folder available"}), 404
    path = Path(folder).resolve()
    if not is_path_under_project(path):
        return jsonify({"error": "Output path is outside the project"}), 403

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        return jsonify({"error": f"Could not open folder: {exc}"}), 500
    return jsonify({"ok": True, "path": str(path)})


def main():
    app.run(host="127.0.0.1", port=5050, debug=True, threaded=True)


if __name__ == "__main__":
    main()
