"""Buddha Air daily flight-log parser."""

from __future__ import annotations

import re

AC_REG_RE = re.compile(r"^9N-?[A-Z]{2,3}$", re.IGNORECASE)
SECTOR_RE = re.compile(r"^([A-Z]{3})\s*[-–—/]\s*([A-Z]{3})$", re.IGNORECASE)
FLT_RE = re.compile(r"^(?:SHA|SH|HA|U4|YT)?\s*(\d{3,4})$", re.IGNORECASE)
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
INT_RE = re.compile(r"^\d{1,3}$")


def _normalize_reg(raw: str) -> str:
    raw = raw.upper().replace(" ", "")
    if raw.startswith("9N") and "-" not in raw and len(raw) >= 5:
        return f"9N-{raw[2:]}"
    return raw.upper()


def _normalize_flt(raw: str) -> str:
    m = FLT_RE.match(raw.strip())
    if not m:
        return raw.strip().upper()
    return f"SHA {m.group(1)}"


def _normalize_time(raw: str) -> str:
    m = TIME_RE.match(raw.strip().replace(" ", ""))
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _assign_regs(items: list[dict], row_ys: list[float]) -> dict[float, str]:
    regs = []
    for it in items:
        t = it["text"].upper().replace(" ", "")
        if AC_REG_RE.match(t) or t == "9N":
            if t == "9N":
                continue
            regs.append((_normalize_reg(it["text"]), it["cy"], it["y1"], it["y2"]))
        elif re.fullmatch(r"[A-Z]{3}", t) and t.startswith("A"):
            for other in items:
                if abs(other["cy"] - it["cy"]) < 20 and other["text"].upper().strip() == "9N":
                    regs.append(
                        (
                            f"9N-{t}",
                            it["cy"],
                            min(it["y1"], other["y1"]),
                            max(it["y2"], other["y2"]),
                        )
                    )
                    break

    regs.sort(key=lambda r: r[1])
    mapping: dict[float, str] = {}
    for y in row_ys:
        best = ""
        best_dist = 1e9
        for reg, cy, y1, y2 in regs:
            dist = 0 if y1 - 8 <= y <= y2 + 8 else abs(cy - y)
            if dist < best_dist:
                best_dist = dist
                best = reg
        mapping[y] = best
    return mapping


class BuddhaPlugin:
    name = "Buddha"

    def parse_rows(self, items: list[dict]) -> list[dict[str, str]]:
        flt_items = [
            it for it in items if FLT_RE.match(it["text"].strip()) and it["cx"] > 700
        ]
        if not flt_items:
            flt_items = [it for it in items if SECTOR_RE.match(it["text"].strip())]

        flt_items.sort(key=lambda z: z["cy"])
        anchors: list[dict] = []
        for it in flt_items:
            if anchors and abs(anchors[-1]["cy"] - it["cy"]) < 10:
                continue
            anchors.append(it)

        row_ys = [a["cy"] for a in anchors]
        reg_map = _assign_regs(items, row_ys)
        rows: list[dict[str, str]] = []

        for anchor in anchors:
            y = anchor["cy"]
            band = [it for it in items if abs(it["cy"] - y) <= 11]
            band.sort(key=lambda z: -z["cx"])

            flt_no = ""
            sector = ""
            times: list[str] = []
            pob: list[str] = []
            scores: list[float] = []

            for it in band:
                raw = it["text"].strip()
                upper = raw.upper()
                scores.append(float(it.get("score") or 0))

                if not flt_no and FLT_RE.match(raw) and it["cx"] > 700:
                    flt_no = _normalize_flt(raw)
                    continue
                if not sector and SECTOR_RE.match(upper):
                    m = SECTOR_RE.match(upper)
                    sector = f"{m.group(1)}-{m.group(2)}"
                    continue
                if AC_REG_RE.match(upper.replace(" ", "")):
                    continue
                if TIME_RE.match(raw.replace(" ", "")):
                    times.append(_normalize_time(raw))
                    continue
                if INT_RE.match(raw) and it["cx"] < 450:
                    val = int(raw)
                    if val <= 200:
                        pob.append(str(val))
                    continue

            times = (times + [""] * 6)[:6]
            pob = (pob + [""] * 6)[:6]
            if not flt_no and not sector:
                continue

            avg = sum(scores) / len(scores) if scores else 0.7
            rows.append(
                {
                    "ac_reg": reg_map.get(y, ""),
                    "flt_no": flt_no,
                    "sector": sector,
                    "std": times[0],
                    "bc": times[1],
                    "dc": times[2],
                    "takeoff": times[3],
                    "atd": times[4],
                    "ata": times[5],
                    "pob1_adult": pob[0],
                    "pob1_child": pob[1],
                    "pob1_infant": pob[2],
                    "pob2_adult": pob[3],
                    "pob2_child": pob[4],
                    "pob2_infant": pob[5],
                    "_avg_score": str(avg),
                }
            )
        return rows
