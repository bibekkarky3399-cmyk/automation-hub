#!/usr/bin/env python3
"""
Airline flight-log OCR → CSV (thin router).

Configured from config/scripts.json.

  --airlines     Buddha | Yeti
  --input-mode   folder | file
  --images       folder of images (batch)
  --image        single image file
  --output       output folder for UUID CSVs + manifests
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/ocr/main.py` without installing as a package
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.ocr.common import (  # noqa: E402
    IMAGE_EXTENSIONS,
    apply_row_qc,
    build_qc_summary,
    extract_declared_flight_total,
    list_images,
    new_artifact_id,
    run_ocr_items,
    write_csv_with_contract,
)
from scripts.ocr.plugins import get_plugin  # noqa: E402


def resolve_images(args: argparse.Namespace) -> list[Path]:
    mode = (args.input_mode or "folder").strip().lower()
    if mode == "file":
        if not args.image:
            raise ValueError("Single-file mode requires --image")
        path = Path(args.image).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Image file does not exist: {path}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"Unsupported image type: {path.suffix}")
        return [path]

    if not args.images:
        raise ValueError("Folder mode requires --images")
    folder = Path(args.images).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Image folder does not exist: {folder}")
    images = list_images(folder)
    if not images:
        raise ValueError(f"No images found in {folder}")
    return images


def process_image(image_path: Path, airline: str, output_dir: Path, max_bytes: int) -> Path:
    print(f"Processing: {image_path.name}")
    size = image_path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"Image exceeds max size ({size} > {max_bytes} bytes): {image_path.name}"
        )

    items = run_ocr_items(image_path)
    print(f"  ocr_tokens={len(items)}")

    plugin = get_plugin(airline)
    raw_rows = plugin.parse_rows(items)
    declared = extract_declared_flight_total(items)

    rows = []
    for row in raw_rows:
        avg = float(row.pop("_avg_score", 0.7) or 0.7)
        rows.append(apply_row_qc(row, avg_token_score=avg))

    qc = build_qc_summary(rows, declared)
    print(
        f"QC: rows={qc['rows_total']} flagged={qc['rows_flagged']} "
        f"low_conf={qc['rows_low_confidence']} "
        f"declared={qc.get('declared_flight_total')} "
        f"mismatch={qc['row_count_mismatch']}"
    )
    print(f"rows_total={qc['rows_total']}")

    out_path = output_dir / f"{new_artifact_id()}.csv"
    write_csv_with_contract(
        out_path,
        rows,
        airline=plugin.name,
        source_image=image_path.name,
        qc_summary=qc,
    )
    print(f"  → csv={out_path.name} rows={len(rows)}")
    print(f"CSV: {out_path.resolve()}")
    manifest_path = out_path.with_suffix(".manifest.json")
    # Quote values so filenames with spaces (e.g. "inputimage2 copy.png") parse safely.
    print(
        "ARTIFACT: "
        f'source="{image_path.name}" '
        f'source_path="{image_path.resolve()}" '
        f'csv="{out_path.resolve()}" '
        f'manifest="{manifest_path.resolve()}" '
        f"rows={len(rows)}"
    )
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Airline flight-log OCR → CSV")
    parser.add_argument("--airlines", required=True, help="Airline plugin: Buddha or Yeti")
    parser.add_argument(
        "--input-mode",
        default="folder",
        choices=["folder", "file"],
        help="Batch folder or single file",
    )
    parser.add_argument("--images", type=Path, help="Input image folder (batch mode)")
    parser.add_argument("--image", type=Path, help="Single image file (file mode)")
    parser.add_argument("--output", required=True, type=Path, help="Output CSV folder")
    parser.add_argument(
        "--max-image-bytes",
        type=int,
        default=25 * 1024 * 1024,
        help="Reject images larger than this many bytes",
    )
    args = parser.parse_args()

    try:
        images = resolve_images(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Airline: {args.airlines}")
    print(f"Mode:    {args.input_mode}")
    print(f"Images:  {len(images)} file(s)")
    print(f"Output:  {output_dir}")

    written: list[Path] = []
    for image_path in images:
        try:
            written.append(
                process_image(image_path, args.airlines, output_dir, args.max_image_bytes)
            )
        except Exception as exc:
            print(f"  error: {image_path.name}: {exc}", file=sys.stderr)
            return 1

    print(f"\nDone: {len(written)} CSV file(s)")
    if written:
        print(f"CSV: {written[-1].resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
