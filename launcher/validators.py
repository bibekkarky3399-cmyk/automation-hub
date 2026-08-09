"""Input validation for folder paths, files, and permissions."""

from __future__ import annotations

import re
from pathlib import Path

from launcher.config_loader import PROJECT_ROOT

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
UPLOADS_DIR = PROJECT_ROOT / "uploads" / "staged"


def validate_folder(
    path_str: str,
    *,
    require_images: bool = False,
    create_if_missing: bool = False,
) -> dict:
    if not path_str or not str(path_str).strip():
        return {"ok": False, "error": "Please enter a folder path."}

    path = Path(path_str).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        return {"ok": False, "error": f"Invalid path: {exc}"}

    if not path.exists():
        if create_if_missing:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"Could not create folder: {exc}"}
        else:
            return {"ok": False, "error": f"Folder does not exist: {path}"}
    if not path.is_dir():
        return {"ok": False, "error": "Path is not a directory."}

    try:
        entries = list(path.iterdir())
    except PermissionError:
        return {"ok": False, "error": "Permission denied reading this folder."}
    except OSError as exc:
        return {"ok": False, "error": f"Cannot read folder: {exc}"}

    image_count = sum(
        1
        for child in entries
        if child.is_file() and child.suffix.lower() in IMAGE_EXTENSIONS
    )
    if require_images and image_count == 0:
        return {
            "ok": False,
            "error": "No image files found in this folder (PNG, JPG, etc.).",
        }

    return {
        "ok": True,
        "path": str(path),
        "image_count": image_count,
        "under_project": is_path_under_project(path),
    }


def validate_report_path(path: Path) -> bool:
    """Reports must live under project root."""
    try:
        path = path.resolve()
        return path.is_file() and is_path_under_project(path)
    except OSError:
        return False


def is_path_under_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def coerce_bool(value, *, default: bool = False) -> bool:
    """Parse checkbox / JSON / form values into a real bool.

    Importantly, the string "false" must become False — bool("false") is True
    in Python because non-empty strings are truthy.
    """
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "false", "0", "no", "off", "n"}:
            return False
        if normalized in {"true", "1", "yes", "on", "y"}:
            return True
        return bool(default)
    return bool(value)


def validate_text(value: str, *, required: bool = False, pattern: str | None = None) -> dict:
    if not value or not str(value).strip():
        if required:
            return {"ok": False, "error": "This field is required."}
        return {"ok": True, "value": ""}

    value = str(value).strip()
    if pattern and not re.fullmatch(pattern, value):
        return {"ok": False, "error": "Value does not match the required format."}
    return {"ok": True, "value": value}


def validate_select(value: str, options: list, *, required: bool = False) -> dict:
    allowed = set()
    for opt in options:
        if isinstance(opt, str):
            allowed.add(opt)
        elif isinstance(opt, dict):
            allowed.add(opt.get("value", ""))

    if not value or not str(value).strip():
        if required:
            return {"ok": False, "error": "Please select an option."}
        return {"ok": True, "value": ""}

    value = str(value).strip()
    if value not in allowed:
        return {"ok": False, "error": "Invalid selection."}
    return {"ok": True, "value": value}


def validate_file_path(path_str: str, *, accept: str | None = None, required: bool = False) -> dict:
    if not path_str or not str(path_str).strip():
        if required:
            return {"ok": False, "error": "Please upload a file."}
        return {"ok": True, "path": ""}

    path = Path(path_str).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        return {"ok": False, "error": f"Invalid path: {exc}"}

    if not path.is_file():
        return {"ok": False, "error": "File does not exist."}

    if not is_path_under_project(path):
        return {"ok": False, "error": "File must be within the project directory."}

    if accept:
        extensions = {e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
                      for e in accept.split(",") if e.strip()}
        if extensions and path.suffix.lower() not in extensions:
            allowed = ", ".join(sorted(extensions))
            return {"ok": False, "error": f"File type not allowed. Accepted: {allowed}"}

    return {"ok": True, "path": str(path), "filename": path.name}


def ensure_uploads_dir() -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOADS_DIR
