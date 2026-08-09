"""Shared OCR helpers, export contract, and QC."""

from __future__ import annotations

import csv
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# Stable export contract — bump when columns change incompatibly
SCHEMA_VERSION = "1.0.0"

CSV_HEADERS = [
    "ac_reg",
    "flt_no",
    "sector",
    "std",
    "bc",
    "dc",
    "takeoff",
    "atd",
    "ata",
    "pob1_adult",
    "pob1_child",
    "pob1_infant",
    "pob2_adult",
    "pob2_child",
    "pob2_infant",
    "confidence",
    "qc_flags",
]

TOTAL_FLT_RE = re.compile(
    r"TOTAL\s*FLT\s*(?:COMPLETED)?\s*[-–—:]?\s*(\d+)",
    re.IGNORECASE,
)


def list_images(folder: Path) -> list[Path]:
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def run_ocr_items(image_path: Path) -> list[dict]:
    """OCR with bounding boxes. Each item: text, score, cx, cy, x1, y1, x2, y2."""
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR is required. Install with: pip install paddleocr paddlepaddle"
        ) from exc

    if not hasattr(run_ocr_items, "_engine"):
        run_ocr_items._engine = PaddleOCR(
            use_doc_orientation_classify=True,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            lang="en",
        )

    result = run_ocr_items._engine.predict(str(image_path))
    if not result:
        return []

    page = result[0]
    texts = list(page["rec_texts"] or [])
    boxes = np.asarray(page["rec_boxes"])
    scores = list(page.get("rec_scores") or [])
    if boxes.size == 0 or not texts:
        return []

    items: list[dict] = []
    for idx, (text, box) in enumerate(zip(texts, boxes)):
        text = str(text).strip()
        if not text:
            continue
        coords = np.asarray(box).reshape(-1)
        x1, y1, x2, y2 = map(float, coords[:4])
        score = float(scores[idx]) if idx < len(scores) else 0.0
        items.append(
            {
                "text": text,
                "score": score,
                "cx": (x1 + x2) / 2,
                "cy": (y1 + y2) / 2,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
        )
    return items


def extract_declared_flight_total(items: list[dict]) -> int | None:
    blob = " ".join(it["text"] for it in items)
    m = TOTAL_FLT_RE.search(blob)
    return int(m.group(1)) if m else None


def apply_row_qc(row: dict[str, str], avg_token_score: float | None = None) -> dict[str, str]:
    """Attach confidence + qc_flags to a parsed row."""
    flags: list[str] = []
    required = ("flt_no", "sector", "std")
    for key in required:
        if not str(row.get(key, "")).strip():
            flags.append(f"missing_{key}")

    times = [row.get(k, "") for k in ("std", "bc", "dc", "takeoff", "atd", "ata")]
    filled_times = sum(1 for t in times if t)
    if filled_times < 3:
        flags.append("sparse_times")

    if not row.get("ac_reg"):
        flags.append("missing_ac_reg")

    conf = avg_token_score if avg_token_score is not None else 0.75
    if flags:
        conf = min(conf, 0.55)
    if "missing_sector" in flags or "missing_flt_no" in flags:
        conf = min(conf, 0.4)

    out = dict(row)
    out["confidence"] = f"{conf:.2f}"
    out["qc_flags"] = "|".join(flags) if flags else ""
    return out


def build_qc_summary(rows: list[dict[str, str]], declared_total: int | None) -> dict[str, Any]:
    flagged = [r for r in rows if r.get("qc_flags")]
    low_conf = [r for r in rows if float(r.get("confidence") or 0) < 0.6]
    summary: dict[str, Any] = {
        "rows_total": len(rows),
        "rows_flagged": len(flagged),
        "rows_low_confidence": len(low_conf),
        "declared_flight_total": declared_total,
        "row_count_mismatch": False,
    }
    if declared_total is not None and declared_total != len(rows):
        summary["row_count_mismatch"] = True
        summary["mismatch_detail"] = f"declared={declared_total} parsed={len(rows)}"
    return summary


def write_csv_with_contract(
    path: Path,
    rows: list[dict[str, str]],
    *,
    airline: str,
    source_image: str,
    qc_summary: dict[str, Any],
) -> Path:
    """Write plain CSV (header + rows) plus sidecar manifest.json for metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact_id = path.stem

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in CSV_HEADERS})

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "airline": airline,
        "source_image": source_image,
        "csv_path": str(path.resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "columns": CSV_HEADERS,
        "qc": qc_summary,
    }
    manifest_path = path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def new_artifact_id() -> str:
    return str(uuid.uuid4())
