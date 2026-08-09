"""Yeti Airlines flight-log parser (shared CSV schema)."""

from __future__ import annotations

import re

SECTOR_RE = re.compile(r"^([A-Z]{3})\s*[-–—/]\s*([A-Z]{3})$", re.IGNORECASE)
FLT_RE = re.compile(r"^(?:SHA|SH|HA|U4|YT)?\s*(\d{3,4})$", re.IGNORECASE)
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def _normalize_flt(raw: str) -> str:
    m = FLT_RE.match(raw.strip())
    if not m:
        return raw.strip().upper()
    return m.group(0).upper().replace("  ", " ")


def _normalize_time(raw: str) -> str:
    m = TIME_RE.match(raw.strip().replace(" ", ""))
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


class YetiPlugin:
    name = "Yeti"

    def parse_rows(self, items: list[dict]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        sectors = [it for it in items if SECTOR_RE.match(it["text"].strip())]
        for sec in sectors:
            y = sec["cy"]
            band = [it for it in items if abs(it["cy"] - y) <= 12]
            band.sort(key=lambda z: z["cx"])
            m = SECTOR_RE.match(sec["text"].strip().upper())
            times = [
                _normalize_time(it["text"])
                for it in band
                if TIME_RE.match(it["text"].replace(" ", ""))
            ]
            times = (times + [""] * 6)[:6]
            flt = next((it["text"] for it in band if FLT_RE.match(it["text"].strip())), "")
            scores = [float(it.get("score") or 0) for it in band]
            avg = sum(scores) / len(scores) if scores else 0.7
            rows.append(
                {
                    "ac_reg": "",
                    "flt_no": _normalize_flt(flt) if flt else "",
                    "sector": f"{m.group(1)}-{m.group(2)}" if m else "",
                    "std": times[0],
                    "bc": times[1],
                    "dc": times[2],
                    "takeoff": times[3],
                    "atd": times[4],
                    "ata": times[5],
                    "pob1_adult": "",
                    "pob1_child": "",
                    "pob1_infant": "",
                    "pob2_adult": "",
                    "pob2_child": "",
                    "pob2_infant": "",
                    "_avg_score": str(avg),
                }
            )
        return rows
