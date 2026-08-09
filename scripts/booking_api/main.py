#!/usr/bin/env python3
"""United Solutions Booking API → CSV collector (PlazmaTech SOAP).

Pulls reference / commercial data from the Booking API and writes a CSV.
Does not reserve seats or issue tickets.

Modes:
  sales          SalesReport for a date range
  availability   FlightAvailability for a sector + date
  sectors        SectorCode list
  balance        CheckBalance for one or all airlines

Examples:
  python scripts/booking_api/main.py --mode sectors --output outputs
  python scripts/booking_api/main.py --mode availability \\
    --sector-from KTM --sector-to BDP --flight-date 10-AUG-2026 \\
    --output outputs
  python scripts/booking_api/main.py --mode sales \\
    --from-date 01-JAN-2024 --to-date 31-JAN-2024 \\
    --output outputs
"""

from __future__ import annotations

import argparse
import uuid
import csv
import html
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOAP_NS = "http://booking.us.org/"
DEFAULT_ENDPOINT = "http://dev.usbooking.org/us/UnitedSolutions"

# Airline ids from the API guide (CheckBalance).
AIRLINE_CHOICES = ("ALL", "U4", "S1", "RMK", "YT", "GA", "SHA", "ST")

SALES_FIELDS = [
    "PnrNo",
    "Airline",
    "IssueDate",
    "FlightNo",
    "FlightDate",
    "SectorPair",
    "ClassCode",
    "TicketNo",
    "PassengerName",
    "Nationality",
    "PaxType",
    "Currency",
    "Fare",
    "FSC",
    "TAX",
]

AVAIL_FIELDS = [
    "Direction",
    "Airline",
    "FlightDate",
    "FlightNo",
    "Departure",
    "DepartureTime",
    "Arrival",
    "ArrivalTime",
    "AircraftType",
    "Adult",
    "Child",
    "Infant",
    "FlightId",
    "FlightClassCode",
    "Currency",
    "AdultFare",
    "ChildFare",
    "InfantFare",
    "ResFare",
    "FuelSurcharge",
    "Tax",
    "AdultVAT",
    "ChildVAT",
    "Refundable",
    "FreeBaggage",
    "AgencyCommission",
    "ChildCommission",
    "CallingStationId",
    "CallingStation",
]

SECTOR_FIELDS = ["SectorCode", "SectorName"]
BALANCE_FIELDS = ["AirlineName", "AgencyName", "BalanceAmount"]


@dataclass
class Config:
    mode: str
    endpoint: str
    user_id: str
    password: str
    agency_id: str
    airline_id: str
    sector_from: str
    sector_to: str
    flight_date: str
    return_date: str
    trip_type: str
    nationality: str
    adults: int
    children: int
    client_ip: str
    from_date: str
    to_date: str
    output: Path
    timeout: float


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(el: ET.Element, name: str) -> str:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    # Case-insensitive fallback (API occasionally mistypes tags).
    want = name.lower()
    for child in el:
        if _local(child.tag).lower() == want:
            return (child.text or "").strip()
    # Known typo in SalesReport docs: PassengerMame
    if name == "PassengerName":
        for child in el:
            if _local(child.tag).lower() in {"passengermame", "passengername"}:
                return (child.text or "").strip()
    return ""


def extract_return_xml(soap_xml: str) -> str:
    """Pull inner XML from SOAP <return> (often HTML-escaped) or surface <Error>."""
    # Prefer an explicit Error element anywhere.
    err = re.search(r"<Error>(.*?)</Error>", soap_xml, flags=re.I | re.S)
    if err:
        msg = html.unescape(err.group(1)).strip()
        if msg:
            raise RuntimeError(msg)

    m = re.search(r"<return[^>]*>(.*?)</return>", soap_xml, flags=re.I | re.S)
    if not m:
        raise RuntimeError("SOAP response missing <return> payload")
    inner = html.unescape(m.group(1)).strip()
    if not inner:
        return ""
    # Nested error inside return CDATA payload
    err2 = re.search(r"<Error>(.*?)</Error>", inner, flags=re.I | re.S)
    if err2:
        msg = html.unescape(err2.group(1)).strip()
        if msg:
            raise RuntimeError(msg)
    return inner


def soap_call(endpoint: str, operation: str, fields: dict[str, str], timeout: float) -> str:
    body_lines = []
    for key, value in fields.items():
        body_lines.append(f"         <{key}>{_xml_escape(value)}</{key}>")
    body = "\n".join(body_lines)
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:book="{SOAP_NS}">
   <soapenv:Header/>
   <soapenv:Body>
      <book:{operation}>
{body}
      </book:{operation}>
   </soapenv:Body>
</soapenv:Envelope>"""
    req = urlrequest.Request(
        endpoint,
        data=envelope.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{SOAP_NS}{operation}"',
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SOAP HTTP {exc.code}: {raw[:400]}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"SOAP connection failed: {exc.reason}") from exc


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def parse_rows(payload_xml: str, item_tag: str, fieldnames: list[str], extras: dict[str, str] | None = None) -> list[dict[str, str]]:
    if not payload_xml.strip():
        return []
    try:
        root = ET.fromstring(payload_xml)
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse API XML: {exc}") from exc

    rows: list[dict[str, str]] = []
    for el in root.iter():
        if _local(el.tag) != item_tag:
            continue
        row = {name: _child_text(el, name) for name in fieldnames}
        if extras:
            for k, v in extras.items():
                row.setdefault(k, v)
        # Drop totally empty rows
        if any(v for k, v in row.items() if k not in (extras or {})):
            rows.append(row)
    return rows


def collect_sectors(cfg: Config) -> tuple[list[dict[str, str]], list[str]]:
    print("📡 Calling SectorCode…")
    raw = soap_call(cfg.endpoint, "SectorCode", {"strUserId": cfg.user_id}, cfg.timeout)
    payload = extract_return_xml(raw)
    rows = parse_rows(payload, "Sector", SECTOR_FIELDS)
    print(f"  sectors={len(rows)}")
    return rows, SECTOR_FIELDS


def collect_balance(cfg: Config) -> tuple[list[dict[str, str]], list[str]]:
    airlines = list(AIRLINE_CHOICES[1:]) if cfg.airline_id.upper() == "ALL" else [cfg.airline_id.upper()]
    rows: list[dict[str, str]] = []
    for airline in airlines:
        print(f"📡 Calling CheckBalance ({airline})…")
        raw = soap_call(
            cfg.endpoint,
            "CheckBalance",
            {"strUserId": cfg.user_id, "strAirlineId": airline},
            cfg.timeout,
        )
        payload = extract_return_xml(raw)
        part = parse_rows(payload, "Airline", BALANCE_FIELDS, extras={"RequestedAirlineId": airline})
        print(f"  rows={len(part)}")
        rows.extend(part)
    fields = ["RequestedAirlineId", *BALANCE_FIELDS]
    return rows, fields


def collect_sales(cfg: Config) -> tuple[list[dict[str, str]], list[str]]:
    print("📡 Calling SalesReport…")
    print(f"  from={cfg.from_date} to={cfg.to_date}")
    raw = soap_call(
        cfg.endpoint,
        "SalesReport",
        {
            "strUserId": cfg.user_id,
            "strPassword": cfg.password,
            "strAgencyId": cfg.agency_id,
            "strFromDate": cfg.from_date,
            "strToDate": cfg.to_date,
        },
        cfg.timeout,
    )
    payload = extract_return_xml(raw)
    rows = parse_rows(payload, "TicketDetail", SALES_FIELDS)
    print(f"  tickets={len(rows)}")
    return rows, SALES_FIELDS


def collect_availability(cfg: Config) -> tuple[list[dict[str, str]], list[str]]:
    print("📡 Calling FlightAvailability…")
    print(
        f"  {cfg.sector_from}→{cfg.sector_to} date={cfg.flight_date} "
        f"trip={cfg.trip_type} pax={cfg.adults}A/{cfg.children}C"
    )
    fields = {
        "strUserId": cfg.user_id,
        "strPassword": cfg.password,
        "strAgencyId": cfg.agency_id,
        "strSectorFrom": cfg.sector_from.upper(),
        "strSectorTo": cfg.sector_to.upper(),
        "strFlightDate": cfg.flight_date,
        "strReturnDate": cfg.return_date,
        "strTripType": cfg.trip_type.upper(),
        "strNationality": cfg.nationality.upper(),
        "intAdult": str(cfg.adults),
        "intChild": str(cfg.children),
        "strClientIP": cfg.client_ip or cfg.user_id,
    }
    raw = soap_call(cfg.endpoint, "FlightAvailability", fields, cfg.timeout)
    payload = extract_return_xml(raw)
    if not payload.strip():
        return [], AVAIL_FIELDS

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse availability XML: {exc}") from exc

    rows: list[dict[str, str]] = []
    for direction_el in root.iter():
        direction = _local(direction_el.tag)
        if direction not in {"Outbound", "Inbound"}:
            continue
        for avail in direction_el:
            if _local(avail.tag) != "Availability":
                continue
            row = {name: _child_text(avail, name) for name in AVAIL_FIELDS if name != "Direction"}
            row["Direction"] = direction
            if any(row.get(k) for k in ("FlightNo", "FlightId", "AdultFare")):
                rows.append(row)
    # Fallback: flat Availability nodes without Outbound wrapper
    if not rows:
        rows = parse_rows(payload, "Availability", [f for f in AVAIL_FIELDS if f != "Direction"])
        for row in rows:
            row["Direction"] = "Outbound"
    print(f"  fares/options={len(rows)}")
    return rows, AVAIL_FIELDS


def write_csv(out_dir: Path, mode: str, rows: list[dict[str, str]], fieldnames: list[str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uuid.uuid4()}.csv"
    print(f"\n📝 Writing CSV → {path}")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    print(f"CSV: {path.resolve()}")
    print(
        "ARTIFACT: "
        f'source="Booking API {mode}" '
        f'source_path="{out_dir.resolve()}" '
        f'csv="{path.resolve()}" '
        f"rows={len(rows)}"
    )
    return path


def require_creds(cfg: Config, need_password: bool) -> None:
    missing = []
    if not cfg.user_id:
        missing.append("user_id / BOOKING_API_USER")
    if need_password and not cfg.password:
        missing.append("password / BOOKING_API_PASSWORD")
    if need_password and not cfg.agency_id:
        missing.append("agency_id / BOOKING_API_AGENCY_ID")
    if missing:
        raise RuntimeError("Missing credentials: " + ", ".join(missing))


def to_api_date(value: str) -> str:
    """Normalize Hub ISO dates (YYYY-MM-DD) to SOAP style DD-MMM-YYYY."""
    text = (value or "").strip()
    if not text:
        return ""
    # Already SOAP-style
    if re.match(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$", text):
        return text.upper()
    try:
        return date.fromisoformat(text).strftime("%d-%b-%Y").upper()
    except ValueError:
        pass
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().strftime("%d-%b-%Y").upper()
        except ValueError:
            continue
    raise RuntimeError(
        f"Invalid date '{value}'. Use YYYY-MM-DD (e.g. 2026-08-10)."
    )


def normalize_cfg_dates(cfg: Config) -> None:
    if cfg.flight_date:
        cfg.flight_date = to_api_date(cfg.flight_date)
    if cfg.from_date:
        cfg.from_date = to_api_date(cfg.from_date)
    if cfg.to_date:
        cfg.to_date = to_api_date(cfg.to_date)


def run(cfg: Config) -> int:
    print("=" * 60)
    print("BOOKING API → CSV")
    print("=" * 60)
    print(f"Mode:     {cfg.mode}")
    print(f"Endpoint: {cfg.endpoint}")
    print(f"User:     {cfg.user_id or '(missing)'}")
    print(f"Agency:   {cfg.agency_id or '(n/a)'}")

    try:
        normalize_cfg_dates(cfg)
        if cfg.mode == "sectors":
            require_creds(cfg, need_password=False)
            print("\n🔐 Authenticating…")
            rows, fields = collect_sectors(cfg)
        elif cfg.mode == "balance":
            require_creds(cfg, need_password=False)
            print("\n🔐 Authenticating…")
            rows, fields = collect_balance(cfg)
        elif cfg.mode == "sales":
            require_creds(cfg, need_password=True)
            if not cfg.from_date or not cfg.to_date:
                raise RuntimeError("sales mode requires --from-date and --to-date")
            print("\n🔐 Authenticating…")
            rows, fields = collect_sales(cfg)
        elif cfg.mode == "availability":
            require_creds(cfg, need_password=True)
            if not cfg.sector_from or not cfg.sector_to:
                raise RuntimeError("availability mode requires --sector-from and --sector-to")
            if not cfg.flight_date:
                from datetime import timedelta

                cfg.flight_date = (date.today() + timedelta(days=7)).strftime("%d-%b-%Y").upper()
                print(f"  (flight-date defaulted to {cfg.flight_date})")
            print("\n🔐 Authenticating…")
            rows, fields = collect_availability(cfg)
        else:
            raise RuntimeError(f"Unknown mode: {cfg.mode}")
    except Exception as exc:
        print(f"\n❌ Stopped — {exc}")
        print("SUMMARY: rows=0 failed=1")
        return 1

    write_csv(cfg.output, cfg.mode, rows, fields)
    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    print(f"Mode: {cfg.mode}")
    print(f"Rows: {len(rows)}")
    print(f"SUMMARY: rows={len(rows)} failed=0")
    print(f"rows_total={len(rows)}")
    print("=" * 60)
    # Empty sales/availability is still a successful collection.
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="United Solutions Booking API → CSV collector")
    p.add_argument(
        "--mode",
        choices=["sales", "availability", "sectors", "balance"],
        default="availability",
        help="Which dataset to collect",
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get("BOOKING_API_ENDPOINT", DEFAULT_ENDPOINT),
        help="SOAP endpoint URL (Hub: Settings → Booking API hidden field)",
    )
    p.add_argument(
        "--user-id",
        default=os.environ.get("BOOKING_API_USER", ""),
        help="API user id (Hub: Settings → Booking API)",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("BOOKING_API_PASSWORD", ""),
        help="API password (Hub: Settings → Booking API)",
    )
    p.add_argument(
        "--agency-id",
        default=os.environ.get("BOOKING_API_AGENCY_ID", ""),
        help="Agency id (Hub: Settings → Booking API)",
    )
    p.add_argument(
        "--airline-id",
        default="ALL",
        choices=list(AIRLINE_CHOICES),
        help="For balance mode (ALL = query each airline id)",
    )
    p.add_argument("--sector-from", default="KTM", help="Origin sector code")
    p.add_argument("--sector-to", default="BDP", help="Destination sector code")
    p.add_argument(
        "--flight-date",
        default="",
        help="Flight date DD-MM-YYYY or DD-MON-YYYY (availability)",
    )
    p.add_argument("--return-date", default="", help="Return date for round trips")
    p.add_argument("--trip-type", default="O", choices=["O", "R", "o", "r"])
    p.add_argument("--nationality", default="NP", help="ISO A-2 nationality")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument(
        "--client-ip",
        default=os.environ.get("BOOKING_API_CLIENT_IP", ""),
        help="Client IP / agent marker (Hub: Settings → Booking API)",
    )
    p.add_argument("--from-date", default="", help="Sales report start date")
    p.add_argument("--to-date", default="", help="Sales report end date")
    p.add_argument("--output", type=Path, default=Path("outputs"))
    p.add_argument("--timeout", type=float, default=90.0, help="Per-request timeout seconds")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config(
        mode=args.mode,
        endpoint=(args.endpoint or DEFAULT_ENDPOINT).rstrip("?&").removesuffix("?wsdl"),
        user_id=(args.user_id or "").strip(),
        password=(args.password or "").strip(),
        agency_id=(args.agency_id or "").strip(),
        airline_id=(args.airline_id or "ALL").strip().upper(),
        sector_from=(args.sector_from or "").strip(),
        sector_to=(args.sector_to or "").strip(),
        flight_date=(args.flight_date or "").strip(),
        return_date=(args.return_date or "").strip(),
        trip_type=(args.trip_type or "O").strip().upper(),
        nationality=(args.nationality or "NP").strip().upper(),
        adults=max(0, int(args.adults)),
        children=max(0, int(args.children)),
        client_ip=(args.client_ip or "").strip(),
        from_date=(args.from_date or "").strip(),
        to_date=(args.to_date or "").strip(),
        output=Path(args.output),
        timeout=float(args.timeout),
    )
    # Normalize accidental WSDL URL paste
    if cfg.endpoint.lower().endswith("?wsdl"):
        cfg.endpoint = cfg.endpoint[: -len("?wsdl")]
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
