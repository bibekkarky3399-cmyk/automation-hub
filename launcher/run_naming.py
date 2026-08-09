"""Hub-only run name: form field + display helpers (not passed to scripts)."""

from __future__ import annotations

from typing import Any

RUN_NAME_ID = "run_name"
RUN_NAME_MAX_LEN = 120


def run_name_input(workflow_name: str = "") -> dict[str, Any]:
    """Form field injected at the top of every workflow / chain form."""
    label = (workflow_name or "this job").strip()
    return {
        "id": RUN_NAME_ID,
        "label": "Run name",
        "type": "text",
        "required": True,
        "group": "This run",
        "width": "full",
        "help": "Name this run for History and Active jobs. The workflow type stays visible underneath.",
        "placeholder": f"e.g. Morning {label}",
        "default": "",
        "hub_only": True,
    }


def with_run_name_input(
    inputs: list[dict[str, Any]] | None, *, workflow_name: str = ""
) -> list[dict[str, Any]]:
    """Prepend Run name; drop any existing run_name to avoid duplicates."""
    rest = [
        dict(inp)
        for inp in (inputs or [])
        if str(inp.get("id") or "") != RUN_NAME_ID
    ]
    return [run_name_input(workflow_name), *rest]


def normalize_run_name(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    return text[:RUN_NAME_MAX_LEN]


def extract_run_name(
    parameters: dict[str, Any] | None = None,
    *,
    run: dict[str, Any] | None = None,
    fallback: str = "",
) -> str:
    """Resolve run name from top-level field or parameters."""
    if run:
        top = normalize_run_name(run.get("run_name"))
        if top:
            return top
        parameters = run.get("parameters") or parameters
    name = normalize_run_name((parameters or {}).get(RUN_NAME_ID))
    return name or (fallback or "").strip()


def attach_run_display_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Add run_name, workflow_name, display_name for UI templates."""
    workflow = str(item.get("script_name") or item.get("script_id") or "Workflow").strip()
    run_name = extract_run_name(run=item, fallback="")
    item["workflow_name"] = workflow
    item["run_name"] = run_name
    item["display_name"] = run_name or workflow
    item["has_custom_run_name"] = bool(run_name)
    return item
