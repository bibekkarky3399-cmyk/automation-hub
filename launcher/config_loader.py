"""Load and validate script definitions from config/scripts.json."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "scripts.json"
DELETED_WORKFLOWS_ROOT = PROJECT_ROOT / "backups" / "workflows"
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BACKUP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*__\d{8}T\d{6}Z$")


class ConfigError(Exception):
    pass


def _repair_scripts_json_windows_escapes(raw: str) -> str | None:
    """Fix common Windows path mistakes that make scripts.json invalid JSON.

    Writing `"python": ".venv\\Scripts\\python.exe"` with single backslashes
    yields JSON `Invalid \\escape` (e.g. `\\S`) and breaks public flights.
    Prefer forward slashes, which Windows Python accepts.
    """
    repaired = raw
    # "python": "...anything with backslashes..."
    repaired = re.sub(
        r'("python"\s*:\s*")([^"]*)(")',
        lambda m: m.group(1) + m.group(2).replace("\\", "/") + m.group(3),
        repaired,
        count=1,
    )
    # Comment line often has the same bad Windows example.
    repaired = re.sub(
        r'("_comment_python"\s*:\s*")([^"]*)(")',
        lambda m: m.group(1) + m.group(2).replace("\\", "/") + m.group(3),
        repaired,
        count=1,
    )
    if repaired == raw:
        return None
    try:
        json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return repaired


@lru_cache(maxsize=1)
def load_scripts_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise ConfigError(f"Missing configuration: {CONFIG_PATH}")
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        fixed = _repair_scripts_json_windows_escapes(raw)
        if not fixed:
            raise ConfigError(
                f"Invalid scripts.json ({exc}). On Windows set "
                f'"python": ".venv/Scripts/python.exe" with forward slashes.'
            ) from exc
        CONFIG_PATH.write_text(fixed, encoding="utf-8")
        data = json.loads(fixed)

    from launcher.schema_validator import assert_valid_config

    assert_valid_config(data)
    return data


def reload_scripts_config() -> dict[str, Any]:
    load_scripts_config.cache_clear()
    return load_scripts_config()


def get_scripts_config_raw() -> dict[str, Any]:
    """Return a deep copy of the live config (for Settings editing)."""
    return deepcopy(load_scripts_config())


def save_scripts_config(data: dict[str, Any]) -> dict[str, Any]:
    """Validate, atomically write scripts.json, and reload the in-memory cache.

    No app restart is required — callers get the new config immediately.
    """
    from launcher.schema_validator import assert_valid_config

    if not isinstance(data, dict):
        raise ConfigError("Config payload must be an object")

    # Preserve top-level keys we don't edit in Settings.
    current = get_scripts_config_raw()
    merged = deepcopy(current)
    for key in (
        "scripts",
        "pipelines",
        "python",
        "output_dir",
        "runtime",
        "notifications",
        "schema_version",
    ):
        if key in data:
            merged[key] = deepcopy(data[key])

    assert_valid_config(merged)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(CONFIG_PATH.parent),
        prefix=".scripts.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.replace(CONFIG_PATH)
    return reload_scripts_config()


def _require_valid_id(value: str, *, kind: str = "id") -> str:
    sid = (value or "").strip()
    if not ID_RE.match(sid):
        raise ConfigError(f"Invalid {kind}: use lowercase letters, numbers, underscore (e.g. my_workflow)")
    return sid


def upsert_script(script: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    config = get_scripts_config_raw()
    scripts = list(config.get("scripts") or [])
    sid = _require_valid_id(str(script.get("id") or ""), kind="script id")
    script = deepcopy(script)
    script["id"] = sid

    idx = next((i for i, s in enumerate(scripts) if s.get("id") == sid), -1)
    if create:
        if idx >= 0:
            raise ConfigError(f"Script '{sid}' already exists")
        scripts.append(script)
    else:
        if idx < 0:
            raise ConfigError(f"Unknown script '{sid}'")
        # Keep unknown keys from the existing script; overlay editable fields.
        merged = deepcopy(scripts[idx])
        merged.update(script)
        scripts[idx] = merged

    config["scripts"] = scripts
    return save_scripts_config(config)


def patch_script(script_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    config = get_scripts_config_raw()
    sid = _require_valid_id(script_id, kind="script id")
    scripts = list(config.get("scripts") or [])
    idx = next((i for i, s in enumerate(scripts) if s.get("id") == sid), -1)
    if idx < 0:
        raise ConfigError(f"Unknown script '{sid}'")

    allowed = {
        "name",
        "description",
        "icon",
        "badge",
        "enabled",
        "form_note",
        "timeout_seconds",
        "script",
        "cwd",
        "inputs",
        "stages",
        "progress_hints",
        "fixed_args",
        "summary_keys",
        "summary_patterns",
        "report",
    }
    merged = deepcopy(scripts[idx])
    for key, value in (patch or {}).items():
        if key in allowed:
            merged[key] = deepcopy(value)
    scripts[idx] = merged
    config["scripts"] = scripts
    return save_scripts_config(config)


def _scripts_path(script_rel: str) -> Path | None:
    rel = (script_rel or "").strip()
    if not rel:
        return None
    target = (PROJECT_ROOT / rel).resolve()
    scripts_root = (PROJECT_ROOT / "scripts").resolve()
    try:
        target.relative_to(scripts_root)
    except ValueError:
        return None
    return target


def _backup_workflow(entry: dict[str, Any]) -> dict[str, Any]:
    """Snapshot workflow config + Python files under backups/workflows/."""
    from datetime import datetime, timezone

    sid = str(entry.get("id") or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{sid}__{stamp}"
    backup_dir = DELETED_WORKFLOWS_ROOT / backup_id
    files_dir = backup_dir / "files"
    backup_dir.mkdir(parents=True, exist_ok=True)

    script_rel = str(entry.get("script") or "")
    source = _scripts_path(script_rel)
    copied_from = None
    has_files = False
    if source is not None and source.exists():
        scripts_root = (PROJECT_ROOT / "scripts").resolve()
        if source.name == "main.py" and source.parent != scripts_root and source.parent.is_dir():
            dest = files_dir / source.parent.name
            shutil.copytree(source.parent, dest, dirs_exist_ok=True)
            copied_from = str(source.parent.relative_to(PROJECT_ROOT)) + "/"
        elif source.is_file():
            dest = files_dir / source.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            copied_from = str(source.relative_to(PROJECT_ROOT))
        has_files = True

    workflow_path = backup_dir / "workflow.json"
    workflow_path.write_text(
        json.dumps(entry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    meta = {
        "backup_id": backup_id,
        "id": sid,
        "name": entry.get("name") or sid,
        "description": entry.get("description") or "",
        "badge": entry.get("badge") or "",
        "icon": entry.get("icon") or "bi-lightning-charge",
        "script": script_rel,
        "input_count": len(entry.get("inputs") or []),
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "has_files": has_files,
        "copied_from": copied_from,
        "path": str(backup_dir.relative_to(PROJECT_ROOT)),
    }
    (backup_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return meta


def _remove_workflow_python_files(script_rel: str) -> list[str]:
    """Delete a workflow's Python file/package if it lives under scripts/.

    Returns relative paths that were removed. Refuses anything outside scripts/.
    """
    removed: list[str] = []
    target = _scripts_path(script_rel)
    if target is None or not target.exists():
        return removed

    scripts_root = (PROJECT_ROOT / "scripts").resolve()

    # Package style: scripts/<id>/main.py → remove the whole package folder.
    if target.name == "main.py" and target.parent != scripts_root and target.parent.is_dir():
        package_dir = target.parent
        shutil.rmtree(package_dir)
        removed.append(str(package_dir.relative_to(PROJECT_ROOT)) + "/")
        return removed

    if target.is_file():
        target.unlink()
        removed.append(str(target.relative_to(PROJECT_ROOT)))
        parent = target.parent
        if parent != scripts_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            removed.append(str(parent.relative_to(PROJECT_ROOT)) + "/")
    return removed


def list_deleted_workflows() -> list[dict[str, Any]]:
    """List workflow backups created on delete (newest first)."""
    if not DELETED_WORKFLOWS_ROOT.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in DELETED_WORKFLOWS_ROOT.iterdir():
        if not path.is_dir():
            continue
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta.setdefault("backup_id", path.name)
        meta.setdefault("path", str(path.relative_to(PROJECT_ROOT)))
        items.append(meta)
    items.sort(key=lambda m: str(m.get("deleted_at") or ""), reverse=True)
    return items


def get_deleted_workflow(backup_id: str) -> dict[str, Any]:
    if not BACKUP_ID_RE.match(backup_id or ""):
        raise ConfigError("Invalid backup id")
    backup_dir = DELETED_WORKFLOWS_ROOT / backup_id
    meta_path = backup_dir / "meta.json"
    workflow_path = backup_dir / "workflow.json"
    if not meta_path.is_file() or not workflow_path.is_file():
        raise ConfigError(f"Deleted workflow not found: {backup_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    return {"meta": meta, "workflow": workflow, "path": str(backup_dir.relative_to(PROJECT_ROOT))}


def restore_deleted_workflow(backup_id: str) -> dict[str, Any]:
    """Restore a deleted workflow into scripts.json and scripts/."""
    payload = get_deleted_workflow(backup_id)
    workflow = deepcopy(payload["workflow"])
    sid = _require_valid_id(str(workflow.get("id") or ""), kind="script id")
    config = get_scripts_config_raw()
    if any(s.get("id") == sid for s in config.get("scripts") or []):
        raise ConfigError(
            f"Cannot restore: workflow id '{sid}' already exists. "
            "Rename or delete the current one first."
        )

    script_rel = str(workflow.get("script") or f"scripts/{sid}/main.py")
    dest = _scripts_path(script_rel)
    if dest is None:
        raise ConfigError(f"Cannot restore files outside scripts/: {script_rel}")

    backup_files = DELETED_WORKFLOWS_ROOT / backup_id / "files"
    if backup_files.is_dir() and any(backup_files.iterdir()):
        scripts_root = (PROJECT_ROOT / "scripts").resolve()
        # Prefer package folder copy when present.
        pkg_candidates = [p for p in backup_files.iterdir() if p.is_dir()]
        file_candidates = [p for p in backup_files.iterdir() if p.is_file()]
        if dest.name == "main.py" and dest.parent != scripts_root:
            if dest.parent.exists():
                raise ConfigError(f"Cannot restore: {dest.parent} already exists")
            if pkg_candidates:
                shutil.copytree(pkg_candidates[0], dest.parent)
            elif file_candidates:
                dest.parent.mkdir(parents=True, exist_ok=True)
                for src in file_candidates:
                    shutil.copy2(src, dest.parent / src.name)
            else:
                raise ConfigError("Backup has no files to restore")
        else:
            if dest.exists():
                raise ConfigError(f"Cannot restore: {script_rel} already exists")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if file_candidates:
                shutil.copy2(file_candidates[0], dest)
            elif pkg_candidates:
                main_py = pkg_candidates[0] / "main.py"
                if main_py.is_file():
                    shutil.copy2(main_py, dest)
                else:
                    raise ConfigError("Backup package is missing main.py")
            else:
                raise ConfigError("Backup has no files to restore")

    upsert_script(workflow, create=True)
    return {
        "ok": True,
        "id": sid,
        "backup_id": backup_id,
        "restored_script": script_rel,
    }


def purge_deleted_workflow(backup_id: str) -> dict[str, Any]:
    """Permanently remove a deleted-workflow backup."""
    if not BACKUP_ID_RE.match(backup_id or ""):
        raise ConfigError("Invalid backup id")
    backup_dir = (DELETED_WORKFLOWS_ROOT / backup_id).resolve()
    root = DELETED_WORKFLOWS_ROOT.resolve()
    try:
        backup_dir.relative_to(root)
    except ValueError as exc:
        raise ConfigError("Invalid backup path") from exc
    if not backup_dir.is_dir():
        raise ConfigError(f"Deleted workflow not found: {backup_id}")
    shutil.rmtree(backup_dir)
    return {"ok": True, "backup_id": backup_id}


def delete_script(script_id: str, *, delete_files: bool = True) -> dict[str, Any]:
    config = get_scripts_config_raw()
    sid = _require_valid_id(script_id, kind="script id")
    existing = next((s for s in (config.get("scripts") or []) if s.get("id") == sid), None)
    if existing is None:
        raise ConfigError(f"Unknown script '{sid}'")

    scripts = [s for s in (config.get("scripts") or []) if s.get("id") != sid]
    if not scripts:
        raise ConfigError("Cannot delete the last workflow")

    for pipe in config.get("pipelines") or []:
        for step in pipe.get("steps") or []:
            if step.get("script_id") == sid:
                raise ConfigError(
                    f"Script '{sid}' is used by pipeline '{pipe.get('id')}'. "
                    "Remove or reassign that step first."
                )

    try:
        backup_meta = _backup_workflow(existing)
    except OSError as exc:
        raise ConfigError(f"Could not create backup before delete: {exc}") from exc

    removed_files: list[str] = []
    script_rel = str(existing.get("script") or "")
    config["scripts"] = scripts
    save_scripts_config(config)
    if delete_files:
        try:
            removed_files = _remove_workflow_python_files(script_rel)
        except OSError as exc:
            raise ConfigError(
                f"Workflow backed up and removed from config, but could not delete "
                f"live files for '{script_rel}': {exc}"
            ) from exc
    return {
        "ok": True,
        "id": sid,
        "deleted_files": removed_files,
        "backup": backup_meta,
    }


def upsert_pipeline(pipeline: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    config = get_scripts_config_raw()
    pipelines = list(config.get("pipelines") or [])
    pid = _require_valid_id(str(pipeline.get("id") or ""), kind="pipeline id")
    pipeline = deepcopy(pipeline)
    pipeline["id"] = pid
    if not pipeline.get("steps"):
        raise ConfigError("Pipeline needs at least one step")

    idx = next((i for i, p in enumerate(pipelines) if p.get("id") == pid), -1)
    if create:
        if idx >= 0:
            raise ConfigError(f"Pipeline '{pid}' already exists")
        pipelines.append(pipeline)
    else:
        if idx < 0:
            raise ConfigError(f"Unknown pipeline '{pid}'")
        merged = deepcopy(pipelines[idx])
        merged.update(pipeline)
        pipelines[idx] = merged

    config["pipelines"] = pipelines
    return save_scripts_config(config)


def patch_pipeline(pipeline_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    config = get_scripts_config_raw()
    pid = _require_valid_id(pipeline_id, kind="pipeline id")
    pipelines = list(config.get("pipelines") or [])
    idx = next((i for i, p in enumerate(pipelines) if p.get("id") == pid), -1)
    if idx < 0:
        raise ConfigError(f"Unknown pipeline '{pid}'")

    allowed = {
        "name",
        "description",
        "icon",
        "badge",
        "enabled",
        "form_note",
        "timeout_seconds",
        "steps",
        "inputs",
        "inputs_exclude",
    }
    merged = deepcopy(pipelines[idx])
    for key, value in (patch or {}).items():
        if key in allowed:
            merged[key] = deepcopy(value)
    if not merged.get("steps"):
        raise ConfigError("Pipeline needs at least one step")
    pipelines[idx] = merged
    config["pipelines"] = pipelines
    return save_scripts_config(config)


def delete_pipeline(pipeline_id: str) -> dict[str, Any]:
    config = get_scripts_config_raw()
    pid = _require_valid_id(pipeline_id, kind="pipeline id")
    pipelines = [p for p in (config.get("pipelines") or []) if p.get("id") != pid]
    if len(pipelines) == len(config.get("pipelines") or []):
        raise ConfigError(f"Unknown pipeline '{pid}'")
    config["pipelines"] = pipelines
    return save_scripts_config(config)


def create_script_stub(script_id: str, name: str, description: str = "") -> dict[str, Any]:
    """Create scripts/<id>/main.py + a minimal scripts.json entry."""
    sid = _require_valid_id(script_id, kind="script id")
    script_rel = f"scripts/{sid}/main.py"
    script_path = PROJECT_ROOT / script_rel
    if script_path.exists():
        raise ConfigError(f"Script file already exists: {script_rel}")

    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        f'''#!/usr/bin/env python3
"""Helix workflow stub: {name or sid}.

Replace this file with your automation logic. Print progress lines that match
stages in config/scripts.json, and emit a CSV: line when you write output.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="{name or sid}")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    print("Starting…")
    args.output.mkdir(parents=True, exist_ok=True)
    import uuid
    out = args.output / f"{{uuid.uuid4()}}.csv"
    out.write_text("message\\nhello from {sid}\\n", encoding="utf-8")
    print(f"Writing CSV → {{out}}")
    print(f"CSV: {{out.resolve()}}")
    print("SUMMARY: rows=1 failed=0")
    print("rows_total=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    (script_path.parent / "__init__.py").write_text("", encoding="utf-8")

    entry = {
        "id": sid,
        "name": (name or sid).strip() or sid,
        "description": (description or "").strip(),
        "icon": "bi-lightning-charge",
        "badge": "Draft",
        "script": script_rel,
        "cwd": ".",
        "enabled": True,
        "timeout_seconds": 1800,
        "form_note": "Draft workflow — edit the Python script and form fields in Settings.",
        "inputs": [],
        "stages": [
            {"id": "start", "label": "Start", "match": "Starting"},
            {"id": "write", "label": "Write CSV", "match": "Writing CSV"},
            {"id": "done", "label": "Done", "match": "SUMMARY"},
        ],
        "progress_hints": [
            {"match": "Starting", "label": "Starting…"},
            {"match": "Writing CSV", "label": "Writing CSV…"},
            {"match": "SUMMARY", "label": "Finishing…"},
        ],
        "report": {
            "stdout_line_prefix": "CSV:",
            "glob": "*.csv",
            "pick": "newest_after_start",
            "result_ui": {
                "mode": "run_summary",
                "title": "Results",
                "lead": "Output from this workflow.",
                "stats": "collect",
            },
        },
        "summary_patterns": {
            "report_path": "CSV:\\s*(.+)",
            "rows_total": "rows_total=(\\d+)",
        },
    }
    return upsert_script(entry, create=True)


def get_runtime_config() -> dict[str, Any]:
    config = load_scripts_config()
    runtime = dict(config.get("runtime") or {})
    runtime.setdefault("max_concurrent_jobs", 1)
    runtime.setdefault("default_timeout_seconds", 1800)
    runtime.setdefault("max_image_bytes", 25 * 1024 * 1024)
    return runtime


DEFAULT_OUTPUT_DIR = "outputs"


def get_output_dir(config: dict[str, Any] | None = None) -> Path:
    """Shared run-output folder (UUID CSVs). Relative paths resolve under PROJECT_ROOT."""
    config = config or load_scripts_config()
    raw = str(config.get("output_dir") or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def resolve_python_interpreter(config: dict[str, Any] | None = None) -> Path:
    config = config or load_scripts_config()
    raw = config.get("python") or "python3"
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.is_file():
        return path
    import sys

    return Path(sys.executable)


def get_script_by_id(script_id: str) -> dict[str, Any]:
    config = load_scripts_config()
    for item in config["scripts"]:
        if item.get("id") == script_id:
            return item
    raise KeyError(f"Unknown script id: {script_id}")


def resolve_result_ui(script_id: str | None) -> dict[str, Any]:
    """Return presentation hints for a script's result panel.

    Pipelines use ``pipeline:<id>`` — UI is taken from the last step's script.
    """
    default = {
        "mode": "run_summary",
        "title": "Results",
        "lead": "Preview or download the generated file.",
        "stats": "ocr",
    }
    if not script_id:
        return dict(default)

    try:
        if script_id.startswith("pipeline:"):
            pipeline = get_pipeline_by_id(script_id.split(":", 1)[1], compose_inputs=False)
            steps = pipeline.get("steps") or []
            if not steps:
                return dict(default)
            last_script_id = steps[-1].get("script_id")
            return resolve_result_ui(last_script_id)

        script = get_script_by_id(script_id)
        report = script.get("report") or {}
        ui = dict(report.get("result_ui") or {})
        mode = ui.get("mode") or (
            "per_source" if script_id == "ocr" else "run_summary"
        )
        stats = ui.get("stats") or (
            "ocr" if mode == "per_source" else "booking"
        )
        return {
            "mode": mode,
            "title": ui.get("title") or ("Outputs" if mode == "per_source" else "Results"),
            "lead": ui.get("lead")
            or (
                "Select an image to preview its CSV."
                if mode == "per_source"
                else "Preview or download the generated file."
            ),
            "stats": stats,
        }
    except KeyError:
        return dict(default)


def annotate_pipeline(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Mark a pipeline runnable/blocked based on its own flag and step scripts.

    If any referenced script is disabled or missing, the pipeline is treated as
    disabled and ``disabled_reason`` explains why.
    """
    out = dict(pipeline)
    steps_out: list[dict[str, Any]] = []
    blocked: list[str] = []

    for step in pipeline.get("steps") or []:
        step_copy = dict(step)
        script_id = step_copy.get("script_id") or ""
        label = step_copy.get("label") or script_id
        try:
            script = get_script_by_id(script_id)
            script_enabled = bool(script.get("enabled", True))
            script_name = script.get("name") or script_id
            step_copy["script_enabled"] = script_enabled
            step_copy["script_name"] = script_name
            step_copy["script_missing"] = False
            if not script_enabled:
                if label and label not in {script_name, script_id}:
                    blocked.append(f"{script_name} (step “{label}”)")
                else:
                    blocked.append(script_name)
        except KeyError:
            step_copy["script_enabled"] = False
            step_copy["script_name"] = script_id
            step_copy["script_missing"] = True
            blocked.append(f"missing script “{script_id}” (step “{label}”)")
        steps_out.append(step_copy)

    out["steps"] = steps_out
    config_enabled = bool(pipeline.get("enabled", True))
    out["config_enabled"] = config_enabled
    out["blocked_steps"] = blocked

    if not config_enabled or blocked:
        out["enabled"] = False
        out["disabled_reason"] = "Disabled"
    else:
        out["enabled"] = True
        out["disabled_reason"] = None

    return out


def get_pipeline_by_id(pipeline_id: str, *, compose_inputs: bool = True) -> dict[str, Any]:
    config = load_scripts_config()
    for item in config.get("pipelines") or []:
        if item.get("id") == pipeline_id:
            pipeline = annotate_pipeline(dict(item))
            if compose_inputs:
                from launcher.pipeline_compose import compose_pipeline_inputs

                pipeline["inputs"] = compose_pipeline_inputs(pipeline)
            return pipeline
    raise KeyError(f"Unknown pipeline id: {pipeline_id}")


def list_scripts_public() -> list[dict[str, Any]]:
    config = load_scripts_config()
    out = []
    for s in config["scripts"]:
        out.append(
            {
                "id": s["id"],
                "name": s["name"],
                "description": s.get("description", ""),
                "icon": s.get("icon", "bi-terminal"),
                "badge": s.get("badge", ""),
                "enabled": bool(s.get("enabled", True)),
                "inputs": s.get("inputs", []),
                "timeout_seconds": s.get("timeout_seconds"),
                "form_note": s.get("form_note") or "",
                "stages": s.get("stages") or [],
                "summary_keys": s.get("summary_keys") or [],
                "progress_hints": s.get("progress_hints") or [],
                "result_ui": resolve_result_ui(s["id"]),
            }
        )
    return out


def list_pipelines_public() -> list[dict[str, Any]]:
    from launcher.pipeline_compose import compose_pipeline_inputs

    config = load_scripts_config()
    out = []
    for p in config.get("pipelines") or []:
        pipeline = annotate_pipeline(dict(p))
        composed = compose_pipeline_inputs(pipeline)
        out.append(
            {
                "id": pipeline["id"],
                "name": pipeline["name"],
                "description": pipeline.get("description", ""),
                "icon": pipeline.get("icon", "bi-diagram-3"),
                "badge": pipeline.get("badge", "Pipeline"),
                "enabled": bool(pipeline.get("enabled")),
                "config_enabled": bool(pipeline.get("config_enabled", True)),
                "disabled_reason": pipeline.get("disabled_reason"),
                "blocked_steps": pipeline.get("blocked_steps") or [],
                "inputs": composed,
                "form_note": pipeline.get("form_note") or "",
                "timeout_seconds": pipeline.get("timeout_seconds"),
                "steps": [
                    {
                        "id": s.get("id"),
                        "script_id": s.get("script_id"),
                        "label": s.get("label") or s.get("script_id"),
                        "script_enabled": bool(s.get("script_enabled", True)),
                        "script_name": s.get("script_name") or s.get("script_id"),
                        "script_missing": bool(s.get("script_missing")),
                    }
                    for s in (pipeline.get("steps") or [])
                ],
                "step_count": len(pipeline.get("steps") or []),
                "result_ui": resolve_result_ui(f"pipeline:{pipeline['id']}"),
            }
        )
    return out
