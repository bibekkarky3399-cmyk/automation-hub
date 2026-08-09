"""Validate config/scripts.json against the bundled JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from launcher.config_loader import PROJECT_ROOT, ConfigError

SCHEMA_PATH = PROJECT_ROOT / "config" / "scripts.schema.json"


def validate_scripts_config(data: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings (empty if valid)."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Root config must be an object"]

    if "schema_version" not in data:
        errors.append("Missing required field: schema_version")
    if "python" not in data or not str(data.get("python", "")).strip():
        errors.append("Missing required field: python")
    if "scripts" not in data or not isinstance(data.get("scripts"), list):
        errors.append("Missing required field: scripts (array)")
        return errors
    if not data["scripts"]:
        errors.append("scripts array must not be empty")

    # Prefer jsonschema when available; fall back to lightweight checks
    try:
        import jsonschema
    except ImportError:
        errors.extend(_lightweight_validate(data))
        return errors

    if not SCHEMA_PATH.is_file():
        errors.append(f"Schema file missing: {SCHEMA_PATH}")
        return errors

    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        schema = json.load(fh)

    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{path}: {err.message}")

    # Extra semantic checks
    ids: set[str] = set()
    for i, script in enumerate(data.get("scripts") or []):
        sid = script.get("id")
        if sid in ids:
            errors.append(f"scripts[{i}].id: duplicate id '{sid}'")
        ids.add(sid)
        script_path = PROJECT_ROOT / script.get("script", "")
        if script.get("script") and not script_path.is_file():
            errors.append(f"scripts[{i}].script: file not found: {script_path}")

        input_ids = [inp.get("id") for inp in script.get("inputs") or []]
        if len(input_ids) != len(set(input_ids)):
            errors.append(f"scripts[{i}].inputs: duplicate input ids")

    script_ids = {s.get("id") for s in data.get("scripts") or []}
    pipe_ids: set[str] = set()
    for i, pipe in enumerate(data.get("pipelines") or []):
        pid = pipe.get("id")
        if pid in pipe_ids or pid in script_ids:
            errors.append(f"pipelines[{i}].id: duplicate id '{pid}'")
        pipe_ids.add(pid)
        step_ids: set[str] = set()
        for j, step in enumerate(pipe.get("steps") or []):
            sid = step.get("id")
            if sid in step_ids:
                errors.append(f"pipelines[{i}].steps[{j}].id: duplicate step id '{sid}'")
            step_ids.add(sid)
            ref = step.get("script_id")
            if ref and ref not in script_ids:
                errors.append(
                    f"pipelines[{i}].steps[{j}].script_id: unknown script '{ref}'"
                )

    return errors


def assert_valid_config(data: dict[str, Any]) -> None:
    errors = validate_scripts_config(data)
    if errors:
        raise ConfigError("Invalid scripts.json:\n- " + "\n- ".join(errors))


def _lightweight_validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed_types = {"text", "select", "file", "folder", "boolean"}
    for i, script in enumerate(data.get("scripts") or []):
        for key in ("id", "name", "script", "enabled", "inputs"):
            if key not in script:
                errors.append(f"scripts[{i}]: missing '{key}'")
        for j, inp in enumerate(script.get("inputs") or []):
            if inp.get("type") not in allowed_types:
                errors.append(
                    f"scripts[{i}].inputs[{j}].type: must be one of {sorted(allowed_types)}"
                )
            if inp.get("type") == "select" and not inp.get("options"):
                errors.append(f"scripts[{i}].inputs[{j}]: select requires options")
    return errors
