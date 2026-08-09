#!/usr/bin/env python3
"""
TBO Series Fare — upload CSV inventory via API (Helix).

Auth (from TBO UI after OTP — see series_fare.md):
  TokenId, TraceId, AgencyId, TokenMemberId

Then POST /api/addSeriesFare once per inventory record.

Primary input: PNR results CSV or a full Series Fare sheet.

  python scripts/series_fare/main.py \
    --sheet ./inventory.csv \
    --token-id 'eyJ…' --trace-id '…' \
    --agency-id 73858 --member-id 156197 \
    --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

ADD_SERIES_FARE_URL = (
    "https://seriesfare.travelboutiqueonline.com/api/addSeriesFare"
)
SERIES_FARE_ORIGIN = "https://seriesfare.travelboutiqueonline.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# Auth + Cookie defaults (built in Python — not form inputs).
DEFAULT_AGENCY_ID = "73858"
DEFAULT_MEMBER_ID = "156197"
DEFAULT_FULL_NAME = "Yeti@BLRC498"
DEFAULT_CLIENT_ID = "TBOINDIA"
DEFAULT_PRODUCT_TYPE = "CAR"
DEFAULT_SESSION_FILE = ".tbo_api_session.json"

DEFAULT_FARE_RULE = (
    "This is a Series Fare booking and is Non-Refundable and Non-Changeable. "
    "No-show refunds are not permitted. Passenger name(s) may reflect on the "
    "airline's website one day prior to the departure date. Web check-in can "
    "be completed directly on the airline's website using the PNR and "
    "passenger's last name. Please select the passenger(s) and proceed with "
    "web check-in one day prior to departure after 18:00 hrs. GST details "
    "cannot be added to Series Fare bookings. Addition of any SSR (Special "
    "Service Requests) is not permitted.|this fare is valid for nepalese and "
    "indian nationals only"
)

# TBO enum-ish values from series_fare.md sample
CABIN_CLASS = {"economy": 2, "business": 1, "first": 3, "premium economy": 4}
JOURNEY_TYPE = {"one way": 1, "oneway": 1, "ow": 1, "return": 2, "round trip": 2}
PAX_ADULT, PAX_CHILD, PAX_INFANT = 1, 2, 3


@dataclass
class Config:
    sheet: str
    output: str
    dry_run: bool
    limit: int
    token_id: str
    trace_id: str
    agency_id: str = DEFAULT_AGENCY_ID
    member_id: str = DEFAULT_MEMBER_ID
    full_name: str = DEFAULT_FULL_NAME
    end_user_ip: str = ""
    add_url: str = ADD_SERIES_FARE_URL
    session_file: str = DEFAULT_SESSION_FILE
    airline_code: str = "YT"
    cabin_class: str = "Economy"
    journey_type: str = "One Way"
    dep_time: str = ""
    arr_time: str = ""
    duration: str = ""
    dep_terminal: str = ""
    arr_terminal: str = ""
    baggage: str = "20"
    base_fare: str = ""
    taxes: str = ""
    agent_surcharge: str = "0"
    fare_rules: str = ""
    disable_before_hrs: int = 24
    is_active: int = 1
    is_refundable: int = 0
    inventory_type: int = 1
    geo_type: str = "0"
    is_lcc: str = "0"


@dataclass
class RowResult:
    row: int
    airline: str = ""
    origin: str = ""
    destination: str = ""
    flight_no: str = ""
    travel_from: str = ""
    travel_to: str = ""
    grn: str = ""
    status: str = "failed"
    http_status: str = ""
    message: str = ""


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def norm_key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def rget(row: dict, col: str) -> str:
    return (row.get(norm_key(col), "") or "").strip()


def _put(row: dict, col: str, value: Any) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        row[norm_key(col)] = text


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


DATE_FORMATS = (
    "%Y-%m-%d",
    # Excel/US Series Fare sheets use m/d/Y — must beat d/m/Y.
    "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y",
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y",
    "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d",
)


def norm_date(value: str) -> str:
    """API sample uses YYYY-MM-DD (not Excel-style 8/12/2026)."""
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if " " in text and ":" in text:
        text = text.split(" ")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def baggage_fields(value: str) -> tuple[str, int]:
    """
    TBO working payload uses piece-style codes, not kilograms:
      Segments.Baggage = \"2\", DayAllocation.Baggage = 3
    CSV often has '20kg' (weight) — map that to the working defaults.
    Plain small integers are passed through as piece counts.
    """
    text = str(value or "").strip()
    if not text:
        return "2", 3
    if re.search(r"kg", text, re.I):
        return "2", 3
    m = re.search(r"(\d+)", text)
    if not m:
        return "2", 3
    n = int(m.group(1))
    # Weights look like 15/20/25… — not valid TBO baggage codes.
    if n >= 15:
        return "2", 3
    return str(n), n


def duration_for_api(raw: str) -> str:
    """Working UI sends Duration \"\" when sheet duration is 0/blank."""
    text = str(raw or "").strip()
    if text in ("", "0", "0.0"):
        return ""
    return text


def truthy_flag(value: str, default: str = "0") -> str:
    """Working payload uses IsActive as string \"1\" / \"0\"."""
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "y", "active"):
        return "1"
    if text in ("0", "false", "no", "n", "inactive"):
        return "0"
    return default


def truthy_int(value: str, default: int = 0) -> int:
    return 1 if truthy_flag(str(value), str(default)) == "1" else 0


def airline_api_code(code: str) -> str:
    """Working payload uses 'Yt' (not 'YT')."""
    c = (code or "YT").strip()
    if not c:
        return "Yt"
    return c[0].upper() + c[1:].lower()


def airport_api_code(code: str) -> str:
    """Working payload uses 'KTm' / 'JKr' (first two upper, rest lower)."""
    c = (code or "").strip()
    if not c:
        return c
    if len(c) <= 2:
        return c.upper()
    return c[:2].upper() + c[2:].lower()


def fare_rule_details(sheet_rules: str) -> str:
    """
    Working body prefixes the standard Series Fare disclaimer, then |\"…\".
    """
    custom = (sheet_rules or "").strip()
    if custom.lower().startswith("this is a series fare booking"):
        return custom
    if not custom:
        return DEFAULT_FARE_RULE
    # Match the browser payload: DEFAULT|\"<csv rules>\"
    return f'{DEFAULT_FARE_RULE}|"{custom}"'


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
        return ctx


def http_json(method: str, url: str, payload: dict | None = None,
              headers: dict | None = None, timeout: int = 60) -> tuple[int, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urlrequest.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        code = exc.code
    except urlerror.URLError as exc:
        # macOS Python often lacks CA bundle — retry once unverified so ops
        # can still hit TBO; warn loudly.
        reason = str(exc.reason)
        if "CERTIFICATE_VERIFY_FAILED" not in reason and "SSL" not in reason:
            raise
        print("  WARNING: SSL verify failed — retrying without certificate check")
        ctx = ssl._create_unverified_context()
        try:
            with urlrequest.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                code = resp.getcode() or 200
        except urlerror.HTTPError as exc2:
            raw = exc2.read().decode("utf-8", errors="replace")
            code = exc2.code

    if not raw.strip():
        return code, None
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


def detect_public_ip() -> str:
    """Public IP as seen from the internet (TBO often sends IPv6)."""
    endpoints = (
        "https://api64.ipify.org",   # prefers IPv6 when available
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    )
    for url in endpoints:
        try:
            req = urlrequest.Request(url, headers={"Accept": "text/plain"},
                                     method="GET")
            try:
                with urlrequest.urlopen(req, timeout=8, context=ssl_context()) as r:
                    ip = r.read().decode("utf-8", errors="replace").strip()
            except urlerror.URLError:
                with urlrequest.urlopen(
                    req, timeout=8, context=ssl._create_unverified_context()
                ) as r:
                    ip = r.read().decode("utf-8", errors="replace").strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) or ":" in ip:
                return ip
        except Exception:
            continue
    return ""


def resolve_end_user_ip(configured: str) -> str:
    """Use form/CLI override when set; otherwise detect this machine's public IP."""
    manual = (configured or "").strip()
    if manual and manual not in ("127.0.0.1", "0.0.0.0", "auto", "detect"):
        print(f"EndUserIp: {manual} (from form)")
        return manual
    print("EndUserIp: detecting public IP…")
    ip = detect_public_ip()
    if ip:
        print(f"EndUserIp: {ip}")
        return ip
    print("  WARNING: could not detect public IP — using 127.0.0.1")
    return "127.0.0.1"


def dig(obj: Any, *names: str) -> Any:
    """Find the first matching key (case-insensitive) anywhere in a JSON tree."""
    want = {n.lower() for n in names}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in want and v not in (None, ""):
                return v
        for v in obj.values():
            found = dig(v, *names)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = dig(item, *names)
            if found not in (None, ""):
                return found
    elif isinstance(obj, str):
        text = obj.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return dig(json.loads(text), *names)
            except json.JSONDecodeError:
                return None
    return None


def cabin_class_code(label: str) -> int:
    return CABIN_CLASS.get(norm_key(label), 2)


def journey_type_code(label: str) -> int:
    return JOURNEY_TYPE.get(norm_key(label), 1)


# ═══════════════════════════════════════════════════════════════
# SHEET LOADING
# ═══════════════════════════════════════════════════════════════
def load_rows(path: str) -> list[dict]:
    src = Path(path).expanduser()
    if not src.exists():
        print(f"ERROR: sheet not found: {src}")
        sys.exit(2)

    print(f"Loading sheet: {src.name}")
    if src.suffix.lower() in (".xlsx", ".xls"):
        try:
            import pandas as pd
        except ImportError:
            print("ERROR: Excel needs pandas (pip install pandas openpyxl)")
            sys.exit(2)
        xl = pd.ExcelFile(src)
        sheet = next(
            (s for s in xl.sheet_names
             if "series" in s.lower() or "data" in s.lower()),
            xl.sheet_names[0],
        )
        raw = xl.parse(sheet, dtype=str).fillna("").to_dict("records")
    else:
        with src.open(newline="", encoding="utf-8-sig") as fh:
            raw = list(csv.DictReader(fh))

    rows = []
    for r in raw:
        rr = {norm_key(k): ("" if v is None else str(v).strip())
              for k, v in r.items() if k is not None}
        if any(rr.values()):
            rows.append(rr)
    print(f"  Loaded {len(rows)} data rows")
    return rows


def is_pnr_sheet(rows: list[dict]) -> bool:
    if not rows:
        return False
    keys = set(rows[0])
    return "pnr" in keys and "flightnumber" in keys and (
        "day" in keys or "yearmonth" in keys or "datelabel" in keys
    )


def pnr_travel_date(row: dict) -> str:
    day = row.get("day", "").strip()
    ym = re.sub(r"\D", "", row.get("yearmonth", "").strip())
    if day and len(ym) == 6:
        try:
            return f"{ym[:4]}-{ym[4:6]}-{int(day):02d}"
        except (ValueError, TypeError):
            return ""
    label = row.get("datelabel", "").strip()
    if label and "/" in label:
        left, _, right = label.partition("/")
        ym2 = re.sub(r"\D", "", right)
        if left.isdigit() and len(ym2) == 6:
            try:
                return f"{ym2[:4]}-{ym2[4:6]}-{int(left):02d}"
            except (ValueError, TypeError):
                return ""
    return ""


@dataclass
class InventoryRecord:
    """One addSeriesFare payload source."""
    origin: str
    destination: str
    flight_no: str
    fare_code: str
    travel_from: str
    travel_to: str
    ticket_per_day: str
    currency: str
    days: list[dict] = field(default_factory=list)  # {date, pnr, tickets}
    airline_code: str = ""
    dep_time: str = ""
    arr_time: str = ""
    duration: str = ""
    dep_terminal: str = ""
    arr_terminal: str = ""
    baggage: str = ""
    base_fare: str = ""
    taxes: str = ""
    agent_surcharge: str = ""
    fare_rules: str = ""
    grn: str = ""
    booking_from: str = ""
    booking_to: str = ""
    is_active: int | None = None
    is_refundable: int | None = None
    geo_type: str = ""
    cabin_class: str = ""
    journey_type: str = ""
    disable_before_hrs: int | None = None


def adapt_pnr_sheet(rows: list[dict], cfg: Config) -> list[InventoryRecord]:
    """One CSV row → one addSeriesFare call (no grouping)."""
    records: list[InventoryRecord] = []
    skipped = 0
    for r in rows:
        status = (r.get("status") or "").strip().lower()
        pnr = (r.get("pnr") or "").strip()
        travel = pnr_travel_date(r)
        if status and status not in ("booked", "ok", "success"):
            skipped += 1
            continue
        if not pnr or not travel:
            skipped += 1
            continue
        origin = (r.get("origin") or "").upper()
        dest = (r.get("destination") or "").upper()
        flight = (r.get("flightnumber") or "").strip()
        fare = (r.get("farecode") or "").strip()
        adults = r.get("adults") or "10"
        records.append(InventoryRecord(
            origin=origin,
            destination=dest,
            flight_no=flight,
            fare_code=fare,
            travel_from=travel,
            travel_to=travel,
            ticket_per_day=adults,
            currency=r.get("currency") or "NPR",
            days=[{"date": travel, "pnr": pnr, "tickets": adults}],
            airline_code=cfg.airline_code,
            dep_time=cfg.dep_time,
            arr_time=cfg.arr_time,
            duration=cfg.duration,
            dep_terminal=cfg.dep_terminal or origin.lower(),
            arr_terminal=cfg.arr_terminal or dest.lower(),
            baggage=cfg.baggage,
            base_fare=cfg.base_fare,
            taxes=cfg.taxes,
            agent_surcharge=cfg.agent_surcharge,
            fare_rules=cfg.fare_rules or DEFAULT_FARE_RULE,
            grn=pnr,
            disable_before_hrs=cfg.disable_before_hrs,
        ))
        print(f"  Row → API: {cfg.airline_code}{flight} {origin}-{dest} "
              f"{travel} PNR={pnr}")

    if skipped:
        print(f"  PNR sheet: skipped {skipped} row(s) without a usable booking")
    print(f"  PNR → Series Fare: {len(records)} API call(s) (1 per CSV row)")
    return records


def adapt_full_sheet(rows: list[dict], cfg: Config) -> list[InventoryRecord]:
    """Map a classic Series Fare sheet (one row = one inventory)."""
    records = []
    for r in rows:
        origin = rget(r, "Origin").upper()
        dest = rget(r, "Destination").upper()
        flight = rget(r, "Seg1 Flight No") or rget(r, "Flight No")
        if not (origin and dest and flight):
            continue
        travel_from = norm_date(rget(r, "Travel From"))
        travel_to = norm_date(rget(r, "Travel To")) or travel_from
        booking_from = norm_date(rget(r, "Booking From"))
        booking_to = norm_date(rget(r, "Booking To"))
        pnr = rget(r, "PNR") or rget(r, "GRN")
        tickets = rget(r, "Ticket Per Day") or "1"
        dep = rget(r, "Seg1 Dep Time") or cfg.dep_time
        arr = rget(r, "Seg1 Arr Time") or cfg.arr_time
        # Keep sheet duration as-is (0/blank → API ""). Do not invent minutes.
        dur = rget(r, "Seg1 Duration") or cfg.duration
        bag = rget(r, "Seg1 Baggage") or cfg.baggage
        days = []
        if travel_from and travel_to and travel_from == travel_to:
            days = [{"date": travel_from, "pnr": pnr, "tickets": tickets}]
        elif travel_from and travel_to:
            try:
                start = datetime.strptime(travel_from, "%Y-%m-%d").date()
                end = datetime.strptime(travel_to, "%Y-%m-%d").date()
                cur = start
                while cur <= end:
                    days.append({
                        "date": cur.isoformat(),
                        "pnr": pnr,
                        "tickets": tickets,
                    })
                    cur = cur.fromordinal(cur.toordinal() + 1)
            except ValueError:
                days = [{"date": travel_from, "pnr": pnr, "tickets": tickets}]
        geo = rget(r, "GEO Type") or rget(r, "Geo Type")
        if geo and not geo.isdigit():
            # Sheet uses intl/dom; API sample uses "0".
            geo = "0" if norm_key(geo) in ("intl", "international") else \
                ("1" if norm_key(geo) in ("dom", "domestic") else cfg.geo_type)
        disable_raw = (
            rget(r, "Disable Before Hrs")
            or rget(r, "Disable Before Dept")
            or rget(r, "DisableBeforeDept")
            or rget(r, "Disable Before Departure")
        )
        records.append(InventoryRecord(
            origin=origin,
            destination=dest,
            flight_no=flight,
            fare_code=rget(r, "Seg1 Class") or rget(r, "Class"),
            travel_from=travel_from,
            travel_to=travel_to,
            ticket_per_day=tickets,
            currency=rget(r, "Adult Currency") or "NPR",
            days=days,
            airline_code=rget(r, "Seg1 Airline Code") or rget(r, "Airline")
            or cfg.airline_code,
            dep_time=dep,
            arr_time=arr,
            duration=dur,
            dep_terminal=rget(r, "Seg1 Dep Terminal") or origin.lower(),
            arr_terminal=rget(r, "Seg1 Arr Terminal") or dest.lower(),
            baggage=bag,
            base_fare=rget(r, "Adult Base Fare") or cfg.base_fare,
            taxes=rget(r, "Adult Taxes") or cfg.taxes,
            agent_surcharge=rget(r, "Adult Agency Surcharge")
            or cfg.agent_surcharge,
            fare_rules=rget(r, "Seg1 Fare Rules") or cfg.fare_rules
            or DEFAULT_FARE_RULE,
            grn=rget(r, "GRN") or pnr,
            booking_from=booking_from,
            booking_to=booking_to,
            is_active=truthy_int(rget(r, "Is Active"), cfg.is_active),
            is_refundable=truthy_int(rget(r, "Is Refundable"), cfg.is_refundable),
            geo_type=geo or cfg.geo_type,
            cabin_class=rget(r, "Cabin Class") or cfg.cabin_class,
            journey_type=rget(r, "Journey Type") or cfg.journey_type,
            disable_before_hrs=(
                parse_int(disable_raw, cfg.disable_before_hrs)
                if disable_raw else cfg.disable_before_hrs
            ),
        ))
    print(f"  Full sheet → {len(records)} API call(s) (1 per CSV row)")
    return records


def prepare_records(cfg: Config) -> list[InventoryRecord]:
    rows = load_rows(cfg.sheet)
    if not rows:
        return []
    if is_pnr_sheet(rows):
        print("  Detected PNR CSV — one addSeriesFare POST per booked row")
        return adapt_pnr_sheet(rows, cfg)
    print("  Detected Series Fare sheet — one addSeriesFare POST per row")
    return adapt_full_sheet(rows, cfg)


# ═══════════════════════════════════════════════════════════════
# AUTH + API
# ═══════════════════════════════════════════════════════════════
def load_session(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_session(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def authenticate(cfg: Config) -> tuple[str, str, str, str]:
    """Return (token_id, agency_id, member_id, trace_id).

    Agency/Member come from Python defaults; TokenId/TraceId from form or cache.
    """
    session_path = Path(cfg.session_file).expanduser()
    token = cfg.token_id.strip()
    agency = cfg.agency_id.strip() or DEFAULT_AGENCY_ID
    member = cfg.member_id.strip() or DEFAULT_MEMBER_ID
    trace = cfg.trace_id.strip()

    cached = load_session(session_path)
    if not token:
        token = str(cached.get("TokenId") or "")
    if not trace:
        trace = str(cached.get("TraceId") or "")

    if not token:
        print("ERROR: after OTP in the TBO UI, paste TokenId into the form "
              "(TraceId recommended too).")
        sys.exit(2)

    source = "form" if cfg.token_id.strip() else f"saved ({session_path.name})"
    print(f"Auth: TokenId from {source}")
    print(f"  AgencyId/MemberId from script defaults "
          f"({agency}/{member})")
    if trace:
        print(f"  TraceId: {trace[:36]}{'…' if len(trace) > 36 else ''}")
    else:
        print("  TraceId: (empty)")
    save_session(session_path, {
        "TokenId": token,
        "AgencyId": agency,
        "MemberId": member,
        "TraceId": trace,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    })
    return token, agency, member, trace


def build_payload(rec: InventoryRecord, cfg: Config,
                  token: str, agency: str, member: str,
                  trace: str = "") -> dict:
    airline_api = airline_api_code(rec.airline_code or cfg.airline_code)
    origin = airport_api_code(rec.origin)
    dest = airport_api_code(rec.destination)
    seg_bag, day_bag = baggage_fields(rec.baggage or cfg.baggage)
    dur = duration_for_api(rec.duration if rec.duration is not None else cfg.duration)

    base = rec.base_fare or cfg.base_fare or "0"
    tax = rec.taxes or cfg.taxes or "0"
    surcharge = rec.agent_surcharge or cfg.agent_surcharge or "0"
    fare_block = [
        {"PaxType": PAX_ADULT, "BaseFare": str(base), "Tax": str(tax),
         "AgentSurcharge": str(surcharge)},
        {"PaxType": PAX_CHILD, "BaseFare": str(base), "Tax": str(tax),
         "AgentSurcharge": str(surcharge)},
        {"PaxType": PAX_INFANT, "BaseFare": str(base), "Tax": str(tax),
         "AgentSurcharge": str(surcharge)},
    ]

    segment = {
        "AirlineCode": airline_api,
        "FlightNumber": str(rec.flight_no),
        "Origin": origin,
        "Destination": dest,
        "BookingClass": rec.fare_code or "",
        "DeptTime": rec.dep_time or cfg.dep_time or "",
        "ArrTime": rec.arr_time or cfg.arr_time or "",
        "Duration": dur,
        "DeptTerminal": (rec.dep_terminal or rec.origin).lower(),
        "ArrTerminal": (rec.arr_terminal or rec.destination).lower(),
        "Baggage": seg_bag,
        "FareRuleDetails": fare_rule_details(
            rec.fare_rules or cfg.fare_rules or ""),
    }

    disable = (
        cfg.disable_before_hrs if rec.disable_before_hrs is None
        else rec.disable_before_hrs
    )

    day_alloc = []
    for d in rec.days:
        day_alloc.append({
            "TravelDate": norm_date(d["date"]),
            "PNR": d.get("pnr") or "",
            # Working browser payload sends this as a string.
            "DisableBeforeDept": str(disable),
            "TicketPerDay": parse_int(str(d.get("tickets") or rec.ticket_per_day), 1),
            "TicketLeft": 0,
            "Baggage": day_bag,
            "Status": 1,
            "Segments": [],
            "Fare": [],
        })

    if rec.is_active is None:
        is_active = truthy_flag(str(cfg.is_active), "1")
    else:
        is_active = "1" if rec.is_active else "0"
    is_refundable = (cfg.is_refundable if rec.is_refundable is None
                     else rec.is_refundable)

    return {
        "AgencyId": str(agency),
        "EndUserIp": cfg.end_user_ip or "127.0.0.1",
        "TokenId": token,
        # Working capture often has TraceId as "".
        "TraceId": trace if trace is not None else (cfg.trace_id or ""),
        "InventoryType": cfg.inventory_type,
        "ValidatingAirline": airline_api,
        "Origin": origin,
        "Destination": dest,
        "IsActive": is_active,
        "CabinClass": cabin_class_code(rec.cabin_class or cfg.cabin_class),
        "IsLCC": str(cfg.is_lcc),
        "GeoType": str(rec.geo_type or cfg.geo_type),
        "GRN": rec.grn or (rec.days[0]["pnr"] if rec.days else ""),
        "IsRefundable": is_refundable,
        "JourneyType": journey_type_code(rec.journey_type or cfg.journey_type),
        "TravelFrom": norm_date(rec.travel_from),
        "TravelTo": norm_date(rec.travel_to),
        "BookingFrom": rec.booking_from or "",
        "BookingTo": rec.booking_to or "",
        "CouponCode": "",
        "Segments": [segment],
        "Fare": fare_block,
        "DayAllocation": day_alloc,
        "TokenAgencyId": str(agency),
        "TokenMemberId": str(member),
    }


def account_code_for(agency: str) -> str:
    """Working capture: agency 73858 → accountCode H3858."""
    digits = re.sub(r"\D", "", agency or "")
    if len(digits) >= 4:
        return "H" + digits[1:]
    return ""


def parse_cookie_header(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def build_cookie_header(token: str, agency: str, member: str, end_user_ip: str,
                        *, full_name: str = DEFAULT_FULL_NAME) -> str:
    """Build the Cookie header in Python (not from the Helix form)."""
    ip = (end_user_ip or "").strip()
    jar = {
        "agencyId": agency,
        "memberId": member,
        "tokenId": "",
        "tokenAgencyId": agency,
        "tokenMemberId": member,
        "accountCode": account_code_for(agency),
        "ProductType": DEFAULT_PRODUCT_TYPE,
        "clientID": DEFAULT_CLIENT_ID,
        "FullName": urlparse.quote((full_name or DEFAULT_FULL_NAME).strip(),
                                   safe=""),
        "clientIP": urlparse.quote(ip, safe="") if ip else "",
    }
    if token:
        jar["pTokenId"] = token
    order = [
        "agencyId", "memberId", "tokenId", "tokenAgencyId", "tokenMemberId",
        "accountCode", "ProductType", "clientID", "FullName", "clientIP",
        "pTokenId",
    ]
    return "; ".join(f"{k}={jar[k]}" for k in order if k in jar)


def tbo_browser_headers(token: str, agency: str, member: str,
                        end_user_ip: str,
                        *, full_name: str = DEFAULT_FULL_NAME) -> dict[str, str]:
    """HTTP headers for addSeriesFare — always constructed in this file."""
    return {
        "Accept": "*/*",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": SERIES_FARE_ORIGIN,
        "Referer": f"{SERIES_FARE_ORIGIN}/",
        "User-Agent": BROWSER_UA,
        "Cookie": build_cookie_header(
            token, agency, member, end_user_ip, full_name=full_name,
        ),
    }


def api_history_path(out_dir: Path) -> Path:
    return out_dir / "series_fare_api_history.json"


def append_api_history(history_path: Path, index: int, *, url: str,
                       payload: dict, code: int, resp: Any, ok: bool,
                       headers: dict | None = None) -> Path:
    """Append one addSeriesFare request/response to a shared history JSON."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history: dict[str, Any] = {"calls": []}
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("calls"), list):
                history = loaded
            elif isinstance(loaded, list):
                history = {"calls": loaded}
        except (json.JSONDecodeError, OSError):
            pass

    hdr_dump = None
    if headers:
        hdr_dump = dict(headers)
        jar = parse_cookie_header(hdr_dump.get("Cookie", ""))
        hdr_dump["Cookie"] = "; ".join(
            f"{k}={'<jwt>' if k == 'pTokenId' and v else v}"
            for k, v in jar.items()
        )
    ts = datetime.now().isoformat(timespec="seconds")
    history.setdefault("calls", []).append({
        "row": index,
        "timestamp": ts,
        "url": url,
        "method": "POST",
        "http_status": code,
        "ok": ok,
        "headers": hdr_dump,
        "request": payload,
        "response": resp,
    })
    history["updated_at"] = ts
    history_path.write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return history_path


def post_series_fare(
    cfg: Config, payload: dict, *, history_path: Path, index: int,
) -> tuple[bool, str, int]:
    headers = tbo_browser_headers(
        token=str(payload.get("TokenId") or cfg.token_id or ""),
        agency=str(payload.get("AgencyId") or payload.get("TokenAgencyId")
                   or cfg.agency_id or DEFAULT_AGENCY_ID),
        member=str(payload.get("TokenMemberId") or cfg.member_id
                   or DEFAULT_MEMBER_ID),
        end_user_ip=str(payload.get("EndUserIp") or cfg.end_user_ip or ""),
        full_name=cfg.full_name or DEFAULT_FULL_NAME,
    )
    has_p = "pTokenId=" in headers.get("Cookie", "") and \
        "pTokenId=;" not in headers.get("Cookie", "")
    print(f"    Headers: Origin/Referer + Cookie "
          f"(pTokenId={'yes' if has_p else 'MISSING'}, "
          f"agencyId={payload.get('AgencyId')})")
    code, resp = http_json("POST", cfg.add_url, payload, headers=headers)
    text = json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) \
        else (resp or "")
    print(f"    HTTP {code}")
    preview = text if len(text) <= 500 else text[:500] + "…"
    print(f"    Response: {preview or '(empty)'}")

    ok_flag = dig(resp, "IsSuccess", "isSuccess", "Success", "success", "Status")
    err = dig(resp, "Error", "error", "Message", "message", "ErrorMessage",
              "Errors", "errorMessage", "ResponseMessage")
    # Nested ASP.NET / TBO envelopes
    if ok_flag is None and isinstance(resp, dict):
        ok_flag = dig(resp.get("d"), "IsSuccess", "Success", "success") if \
            isinstance(resp.get("d"), (dict, str)) else None
        err = err or dig(resp.get("d"), "Error", "Message", "ErrorMessage")

    low = text.lower() if isinstance(text, str) else ""
    # 3xx → /ErrorPage is TBO rejecting the call (auth/WAF), not a success body.
    if code >= 400 or 300 <= code < 400:
        ok, msg = False, f"HTTP {code}: {text[:400]}"
    elif "errorpage" in low or low.strip() in {"/errorpage", "errorpage"}:
        ok, msg = False, f"HTTP {code}: {text[:400]}"
    elif ok_flag in (False, "false", "False", 0, "0"):
        ok, msg = False, f"{err or text[:400]}"
    elif isinstance(err, str) and err and "success" not in err.lower() and \
            ok_flag is None and dig(resp, "Error", "error", "ErrorMessage"):
        ok, msg = False, err[:400]
    else:
        # Explicit failure phrases even on HTTP 200
        ok, msg = True, (str(err) if err else text)[:400] or f"HTTP {code} ok"
        for bad in ("unauthorized", "not authenticated", "invalid token",
                    "session expired", "access denied"):
            if bad in low and "success" not in low:
                ok, msg = False, text[:400]
                break

    append_api_history(
        history_path, index, url=cfg.add_url, payload=payload, code=code,
        resp=resp, ok=ok, headers=headers,
    )
    return ok, msg, code


# ═══════════════════════════════════════════════════════════════
# RESULTS
# ═══════════════════════════════════════════════════════════════
def write_results_csv(cfg: Config, results: list[RowResult]) -> Path:
    out_dir = Path(cfg.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uuid.uuid4()}.csv"
    print(f"\nWriting CSV -> {path}")
    fields = ["row", "airline", "origin", "destination", "flight_no",
              "travel_from", "travel_to", "grn", "status", "http_status",
              "message"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: getattr(r, k) for k in fields})
    print(f"CSV: {path}")
    print(f"rows_total={len(results)}")
    print(f'ARTIFACT: source="Results (CSV)" csv="{path}" rows={len(results)}')
    return path


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def run_upload(cfg: Config) -> int:
    records = prepare_records(cfg)
    if not records:
        print("ERROR: no inventory records to upload "
              "(PNR CSV needs status=booked and a non-empty pnr).")
        return 2

    total = len(records)
    limit = cfg.limit if cfg.limit > 0 else total
    if limit < total:
        print(f"  Limit {limit} of {total} row(s) — remaining skipped this run")
    records = records[:limit]
    print(f"  Will call addSeriesFare {len(records)} time(s)")

    cfg.end_user_ip = resolve_end_user_ip(cfg.end_user_ip)

    # TokenId / TraceId from form; Agency/Member/headers from Python defaults.
    token = cfg.token_id.strip()
    agency = cfg.agency_id.strip() or DEFAULT_AGENCY_ID
    member = cfg.member_id.strip() or DEFAULT_MEMBER_ID
    trace = cfg.trace_id.strip()
    if not cfg.dry_run:
        token, agency, member, trace = authenticate(cfg)
    else:
        print("Auth: practice run — TokenId/TraceId from form; "
              "headers built in script (not posted)")
        if not token:
            print("  WARNING: TokenId empty — paste it on the form to preview a real payload")
            token = "DRY_RUN_TOKEN"
        else:
            print(f"  TokenId: {token[:24]}… ({len(token)} chars)")
        print(f"  AgencyId/MemberId: {agency}/{member} (script defaults)")
        if trace:
            print(f"  TraceId: {trace}")
        else:
            print("  WARNING: TraceId empty — paste it on the form if TBO requires it")

    results: list[RowResult] = []
    added = failed = 0
    history_path = api_history_path(Path(cfg.output).expanduser())
    if not cfg.dry_run:
        print(f"  API history → {history_path}")

    for i, rec in enumerate(records, start=1):
        payload = build_payload(rec, cfg, token, agency, member, trace)
        res = RowResult(
            row=i,
            airline=rec.airline_code or cfg.airline_code,
            origin=rec.origin,
            destination=rec.destination,
            flight_no=rec.flight_no,
            travel_from=rec.travel_from,
            travel_to=rec.travel_to,
            grn=payload.get("GRN", ""),
        )
        print(f"\nAPI {i}/{len(records)}: {res.airline}{res.flight_no} "
              f"{res.origin}-{res.destination} "
              f"{res.travel_from}..{res.travel_to} "
              f"GRN={res.grn}")

        if cfg.dry_run:
            out = Path(cfg.output).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            dump = out / f"series_fare_payload_{i}.json"
            dump.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            headers = tbo_browser_headers(
                token=str(payload.get("TokenId") or ""),
                agency=str(payload.get("AgencyId") or DEFAULT_AGENCY_ID),
                member=str(payload.get("TokenMemberId") or DEFAULT_MEMBER_ID),
                end_user_ip=str(payload.get("EndUserIp") or ""),
                full_name=cfg.full_name or DEFAULT_FULL_NAME,
            )
            # Don't dump the full JWT twice in headers file — show cookie keys.
            hdr_dump = dict(headers)
            jar = parse_cookie_header(hdr_dump.get("Cookie", ""))
            hdr_dump["Cookie"] = "; ".join(
                f"{k}={'<jwt>' if k == 'pTokenId' and v else v}"
                for k, v in jar.items()
            )
            hdr_path = out / f"series_fare_headers_{i}.json"
            hdr_path.write_text(json.dumps(hdr_dump, indent=2), encoding="utf-8")
            print(f"    Dry run — payload → {dump.name}, headers → {hdr_path.name}")
            res.status = "dry-run"
            res.message = f"payload={dump.name}"
            added += 1
        else:
            print(f"    POST {cfg.add_url}")
            ok, msg, code = post_series_fare(
                cfg, payload, history_path=history_path, index=i,
            )
            res.http_status = str(code)
            if ok:
                print(f"    Added: {msg}")
                res.status = "added"
                res.message = msg
                added += 1
            else:
                print(f"    ERROR: {msg}")
                res.status = "failed"
                res.message = msg
                failed += 1
        results.append(res)

    print("\nSUMMARY")
    print(f"  added={added} failed={failed}")
    if not cfg.dry_run:
        print(f"  API history → {history_path}")
    write_results_csv(cfg, results)
    print(f"SUMMARY: added={added} failed={failed} duplicates=0 recovered=0")
    return 0 if failed == 0 else 1


def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Upload Series Fare CSV to TBO addSeriesFare API")
    p.add_argument("--sheet", default="", help="PNR CSV or Series Fare sheet")
    p.add_argument("--output", default=os.environ.get("HELIX_OUTPUT", "outputs"))
    p.add_argument("--dry-run", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--limit", default="0",
                   help="Max rows to POST (0 = every CSV row)")
    p.add_argument("--token-id", default=os.environ.get("TBO_TOKEN_ID", ""),
                   help="JWT from TBO UI after OTP")
    p.add_argument("--trace-id", default=os.environ.get("TBO_TRACE_ID", ""),
                   help="TraceId from TBO UI after OTP")
    # Agency/Member/headers are script constants; CLI overrides optional only.
    p.add_argument("--agency-id",
                   default=os.environ.get("TBO_AGENCY_ID", DEFAULT_AGENCY_ID))
    p.add_argument("--member-id",
                   default=os.environ.get("TBO_MEMBER_ID", DEFAULT_MEMBER_ID))
    p.add_argument("--full-name", default=DEFAULT_FULL_NAME)
    p.add_argument("--end-user-ip", default="",
                   help="Override EndUserIp; leave empty to auto-detect public IP")
    p.add_argument("--add-url", default=ADD_SERIES_FARE_URL)
    # Ignored legacy flags (old Helix forms / CheckMFA / cookie paste)
    p.add_argument("--cookie", default="")
    p.add_argument("--account-code", default="")
    p.add_argument("--username", default="")
    p.add_argument("--password", default="")
    p.add_argument("--mfa-type", default="")
    p.add_argument("--check-mfa-url", default="")
    p.add_argument("--session-file", default=DEFAULT_SESSION_FILE)
    p.add_argument("--airline-code", default="YT")
    p.add_argument("--cabin-class", default="Economy")
    p.add_argument("--journey-type", default="One Way")
    p.add_argument("--dep-time", default="")
    p.add_argument("--arr-time", default="")
    p.add_argument("--duration", default="")
    p.add_argument("--dep-terminal", default="")
    p.add_argument("--arr-terminal", default="")
    p.add_argument("--baggage", default="20")
    p.add_argument("--base-fare", default="")
    p.add_argument("--taxes", default="")
    p.add_argument("--agent-surcharge", default="0")
    p.add_argument("--fare-rules", default="")
    p.add_argument("--disable-before-hrs", default="24")
    p.add_argument("--is-active", default="1")
    p.add_argument("--is-refundable", default="0")
    p.add_argument("--inventory-type", default="1")
    p.add_argument("--geo-type", default="0")
    p.add_argument("--is-lcc", default="0")
    # Accept & ignore legacy browser-mode flags so old Helix forms don't crash.
    p.add_argument("--mode", default="enter")
    p.add_argument("--profile-dir", default="")
    p.add_argument("--form-url", default="")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--pause-seconds", default="0")
    p.add_argument("--duplicate-mode", default="skip")
    p.add_argument("--resume-mode", default="resume")
    p.add_argument("--recover", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--alloc-sheet", default="")
    p.add_argument("--use-alloc-sheet", action=argparse.BooleanOptionalAction,
                   default=None)
    p.add_argument("--airline", default="")
    p.add_argument("--login-wait-minutes", default="10")
    args = p.parse_args()

    return Config(
        sheet=args.sheet.strip(),
        output=args.output.strip() or "outputs",
        dry_run=bool(args.dry_run),
        limit=parse_int(args.limit, 0),
        token_id=args.token_id.strip(),
        trace_id=args.trace_id.strip(),
        agency_id=args.agency_id.strip() or DEFAULT_AGENCY_ID,
        member_id=args.member_id.strip() or DEFAULT_MEMBER_ID,
        full_name=args.full_name.strip() or DEFAULT_FULL_NAME,
        end_user_ip=args.end_user_ip.strip(),
        add_url=args.add_url.strip() or ADD_SERIES_FARE_URL,
        session_file=args.session_file.strip() or DEFAULT_SESSION_FILE,
        airline_code=(args.airline_code or args.airline or "YT").strip() or "YT",
        cabin_class=args.cabin_class.strip() or "Economy",
        journey_type=args.journey_type.strip() or "One Way",
        dep_time=args.dep_time.strip(),
        arr_time=args.arr_time.strip(),
        duration=args.duration.strip(),
        dep_terminal=args.dep_terminal.strip(),
        arr_terminal=args.arr_terminal.strip(),
        baggage=args.baggage.strip() or "20",
        base_fare=args.base_fare.strip(),
        taxes=args.taxes.strip(),
        agent_surcharge=args.agent_surcharge.strip() or "0",
        fare_rules=args.fare_rules.strip(),
        disable_before_hrs=parse_int(args.disable_before_hrs, 24),
        is_active=parse_int(args.is_active, 1),
        is_refundable=parse_int(args.is_refundable, 0),
        inventory_type=parse_int(args.inventory_type, 1),
        geo_type=args.geo_type.strip() or "0",
        is_lcc=args.is_lcc.strip() or "0",
    )


def main() -> int:
    cfg = parse_args()
    print("=" * 60)
    print("TBO SERIES FARE — API upload (addSeriesFare)")
    print(f"  dry-run: {cfg.dry_run} | limit: {cfg.limit or 'all'}")
    print("=" * 60)

    if not cfg.sheet:
        print("ERROR: no sheet selected. Upload the PNR results CSV.")
        return 2
    return run_upload(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
