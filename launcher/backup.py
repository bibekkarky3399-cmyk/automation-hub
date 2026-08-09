"""Build a downloadable Helix backup zip (admin)."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from launcher.config_loader import PROJECT_ROOT, get_output_dir

# Project-relative paths included when present.
BACKUP_PATHS: tuple[str, ...] = (
    "config/scripts.json",
    "config/scripts.schema.json",
    "data/run_history.jsonl",
    "scripts",
    "backups",
    "samples",
    ".env.example",
    "requirements.txt",
)

EXCLUDE_DIR_NAMES = {
    ".venv",
    "__pycache__",
    ".git",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}
EXCLUDE_FILE_NAMES = {".env", ".DS_Store"}


def _rel_to_project(path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return None


def _is_excluded(path: Path) -> bool:
    if path.name in EXCLUDE_FILE_NAMES:
        return True
    rel = _rel_to_project(path)
    parts = Path(rel).parts if rel else path.parts
    return any(part in EXCLUDE_DIR_NAMES for part in parts)


def _iter_files(root: Path) -> Iterable[Path]:
    root = root.resolve()
    if root.is_file():
        if not _is_excluded(root):
            yield root
        return
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not _is_excluded(path):
            yield path


def build_backup_zip() -> tuple[bytes, str]:
    """Return (zip_bytes, download_filename)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"helix-backup-{stamp}.zip"
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_lines = [
            "Helix backup",
            f"created_at_utc={stamp}",
            "includes:",
        ]

        roots: list[Path] = [PROJECT_ROOT / rel for rel in BACKUP_PATHS]
        out_dir = get_output_dir()
        if out_dir not in {p.resolve() for p in roots if p.exists()}:
            roots.append(out_dir)

        seen_arc: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            label = _rel_to_project(root) or root.name
            manifest_lines.append(f"  - {label}")
            for file_path in _iter_files(root):
                arcname = _rel_to_project(file_path) or f"external/{file_path.name}"
                if arcname in seen_arc:
                    continue
                seen_arc.add(arcname)
                zf.write(file_path, arcname)

        zf.writestr(
            "BACKUP_MANIFEST.txt",
            "\n".join(manifest_lines)
            + "\n\nExcluded: .env (secrets), README, .venv, __pycache__, .git\n"
            + "Client restore: unpack over a Helix project copy; keep the real .env separate.\n",
        )

    return buf.getvalue(), filename
