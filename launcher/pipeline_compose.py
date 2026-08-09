"""Compose pipeline forms from step script inputs (no duplicate input defs)."""

from __future__ import annotations

import copy
from typing import Any

from launcher.config_loader import get_script_by_id


def _bound_externally(spec: Any) -> bool:
    """True when a bind does not come from the pipeline form (const / prior step)."""
    if isinstance(spec, dict) and "const" in spec:
        return True
    if isinstance(spec, str) and spec.startswith("steps."):
        return True
    return False


def compose_pipeline_inputs(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the pipeline form by pulling inputs from each step's script.

    - Collects inputs in step order from ``script_id``
    - Skips fields that are fully supplied by ``bind`` (const / steps.*)
    - Honors step ``hide_inputs`` and pipeline ``inputs_exclude``
    - Optional pipeline ``inputs`` are appended last (chain-only extras)
    - Duplicate input ids: first definition wins
    """
    seen: set[str] = set()
    composed: list[dict[str, Any]] = []
    exclude = set(pipeline.get("inputs_exclude") or [])

    for step in pipeline.get("steps") or []:
        script_id = step.get("script_id")
        if not script_id:
            continue
        try:
            script = get_script_by_id(script_id)
        except KeyError:
            continue

        bind = step.get("bind") or {}
        hide = set(step.get("hide_inputs") or [])
        # Also hide anything bound from const / previous step output
        for key, spec in bind.items():
            if _bound_externally(spec):
                hide.add(key)

        step_label = step.get("label") or script.get("name") or script_id
        for inp in script.get("inputs") or []:
            inp_id = inp.get("id")
            if not inp_id or inp_id in seen or inp_id in hide or inp_id in exclude:
                continue
            item = copy.deepcopy(inp)
            # Namespace groups under the step so the form reads as a chain
            original_group = (item.get("group") or "").strip()
            item["group"] = (
                f"{step_label} · {original_group}" if original_group else step_label
            )
            item["source_script_id"] = script_id
            item["source_step_id"] = step.get("id")
            composed.append(item)
            seen.add(inp_id)

    # Optional chain-only extras still allowed
    for inp in pipeline.get("inputs") or []:
        inp_id = inp.get("id")
        if not inp_id or inp_id in seen or inp_id in exclude:
            continue
        composed.append(copy.deepcopy(inp))
        seen.add(inp_id)

    return composed


def build_step_parameters(
    step: dict[str, Any],
    *,
    inputs: dict[str, Any],
    step_results: dict[str, Any],
    resolve_bind_fn,
) -> dict[str, Any]:
    """Auto-map form values onto the target script, then apply explicit bind overrides."""
    script = get_script_by_id(step["script_id"])
    params: dict[str, Any] = {}

    # 1) Auto: same input id on the script ← pipeline form value
    for inp in script.get("inputs") or []:
        inp_id = inp.get("id")
        if not inp_id:
            continue
        if inp_id in inputs and inputs[inp_id] not in (None,):
            params[inp_id] = inputs[inp_id]

    # 2) Explicit bind wins (const, steps.*, or inputs.* remap)
    explicit = resolve_bind_fn(
        step.get("bind"),
        inputs=inputs,
        step_results=step_results,
    )
    params.update(explicit)
    return params
