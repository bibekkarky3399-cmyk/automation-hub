"""United Solutions Booking API client for live flight search (shared with CSV job)."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from launcher.config_loader import get_script_by_id

SOAP_NS = "http://booking.us.org/"
DEFAULT_ENDPOINT = "http://dev.usbooking.org/us/UnitedSolutions"

AIRLINE_META = {
    "U4": {"name": "Buddha Air", "color": "#1C6FD1"},
    "S1": {"name": "Saurya Airlines", "color": "#C97F16"},
    "YT": {"name": "Yeti Airlines", "color": "#1C8F5A"},
    "SHA": {"name": "Shree Airlines", "color": "#7C3AED"},
    "ST": {"name": "Sita Air", "color": "#D6484A"},
    "RMK": {"name": "Simrik Airlines", "color": "#0A6E7A"},
    "GA": {"name": "Guna Airlines", "color": "#0F766E"},
}

# Sort order for booking-class codes (API returns FlightClassCode only — no names).
FARE_LADDER = ["G", "E1", "E2", "E3", "S", "R", "Q", "P", "Y", "B", "O", "N"]

AVAIL_FIELDS = [
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
]

_SECTOR_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}
_SECTOR_TTL = 6 * 60 * 60
_SEARCH_CACHE: dict[str, Any] = {"at": 0.0, "key": "", "result": None}
_SEARCH_TTL = 45.0


class BookingClientError(RuntimeError):
    pass


@dataclass
class BookingCreds:
    endpoint: str
    user_id: str
    password: str
    agency_id: str
    client_ip: str
    timeout: float


def _input_default(script: dict[str, Any], field_id: str, fallback: str = "") -> str:
    for inp in script.get("inputs") or []:
        if inp.get("id") == field_id:
            val = inp.get("default")
            if val is None:
                return fallback
            return str(val).strip()
    return fallback


def load_booking_creds() -> BookingCreds:
    try:
        script = get_script_by_id("booking_api")
    except KeyError as exc:
        raise BookingClientError("Flight search is not configured.") from exc

    endpoint = _input_default(script, "endpoint", DEFAULT_ENDPOINT) or DEFAULT_ENDPOINT
    endpoint = endpoint.rstrip("?&")
    if endpoint.lower().endswith("?wsdl"):
        endpoint = endpoint[: -len("?wsdl")]
    user_id = _input_default(script, "user_id")
    password = _input_default(script, "password")
    agency_id = _input_default(script, "agency_id")
    client_ip = _input_default(script, "client_ip") or user_id
    try:
        timeout = float(_input_default(script, "timeout", "90") or 90)
    except ValueError:
        timeout = 90.0

    if not user_id:
        raise BookingClientError("Flight search credentials are not set in Settings.")
    return BookingCreds(
        endpoint=endpoint,
        user_id=user_id,
        password=password,
        agency_id=agency_id,
        client_ip=client_ip,
        timeout=timeout,
    )


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(el: ET.Element, name: str) -> str:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    want = name.lower()
    for child in el:
        if _local(child.tag).lower() == want:
            return (child.text or "").strip()
    return ""


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def extract_return_xml(soap_xml: str) -> str:
    err = re.search(r"<Error>(.*?)</Error>", soap_xml, flags=re.I | re.S)
    if err:
        msg = html.unescape(err.group(1)).strip()
        if msg:
            raise BookingClientError(msg)

    m = re.search(r"<return[^>]*>(.*?)</return>", soap_xml, flags=re.I | re.S)
    if not m:
        raise BookingClientError("Flight search returned an empty response.")
    inner = html.unescape(m.group(1)).strip()
    if not inner:
        return ""
    err2 = re.search(r"<Error>(.*?)</Error>", inner, flags=re.I | re.S)
    if err2:
        msg = html.unescape(err2.group(1)).strip()
        if msg:
            raise BookingClientError(msg)
    return inner


def soap_call(endpoint: str, operation: str, fields: dict[str, str], timeout: float) -> str:
    body_lines = [f"         <{key}>{_xml_escape(value)}</{key}>" for key, value in fields.items()]
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
        try:
            extract_return_xml(raw)
        except BookingClientError:
            raise
        except Exception:
            pass
        raise BookingClientError(f"Flight search HTTP {exc.code}") from exc
    except urlerror.URLError as exc:
        raise BookingClientError(f"Could not reach flight search: {exc.reason}") from exc


def to_api_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
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
    raise BookingClientError(f"Invalid date '{value}'. Use YYYY-MM-DD.")


def parse_rows(
    payload_xml: str, item_tag: str, fieldnames: list[str]
) -> list[dict[str, str]]:
    if not payload_xml.strip():
        return []
    try:
        root = ET.fromstring(payload_xml)
    except ET.ParseError as exc:
        raise BookingClientError("Could not read flight city list.") from exc

    rows: list[dict[str, str]] = []
    for el in root.iter():
        if _local(el.tag) != item_tag:
            continue
        row = {name: _child_text(el, name) for name in fieldnames}
        if any(row.values()):
            rows.append(row)
    return rows


def fetch_sectors(*, force: bool = False) -> list[dict[str, str]]:
    now = time.time()
    if not force and _SECTOR_CACHE["rows"] and now - float(_SECTOR_CACHE["at"]) < _SECTOR_TTL:
        return list(_SECTOR_CACHE["rows"])

    creds = load_booking_creds()
    raw = soap_call(
        creds.endpoint,
        "SectorCode",
        {"strUserId": creds.user_id},
        creds.timeout,
    )
    payload = extract_return_xml(raw)
    rows = parse_rows(payload, "Sector", ["SectorCode", "SectorName"])
    cleaned = [
        {
            "code": (r.get("SectorCode") or "").strip().upper(),
            "name": title_place(
                (r.get("SectorName") or "").strip() or (r.get("SectorCode") or "").strip()
            ),
        }
        for r in rows
        if (r.get("SectorCode") or "").strip()
    ]
    cleaned.sort(key=lambda r: (r["name"].lower(), r["code"]))
    _SECTOR_CACHE["rows"] = cleaned
    _SECTOR_CACHE["at"] = now
    return list(cleaned)


def _money_num(raw: str) -> float | None:
    text = re.sub(r"[^\d.]", "", str(raw or "").replace(",", ""))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int_or_none(raw: str) -> int | None:
    text = re.sub(r"[^\d]", "", str(raw or ""))
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _airline_code(raw: str, flight_no: str) -> str:
    code = (raw or "").strip().upper()
    if code in AIRLINE_META:
        return code
    fn = (flight_no or "").strip().upper()
    for key in sorted(AIRLINE_META, key=len, reverse=True):
        if fn.startswith(key):
            return key
    return code or "XX"


def _minutes_of_day(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    if re.match(r"^\d{1,2}:\d{2}", s):
        parts = s.split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1][:2])
        except ValueError:
            return None
    digits = re.sub(r"\D", "", s)
    if len(digits) in {3, 4}:
        digits = digits.zfill(4)
        try:
            return int(digits[:2]) * 60 + int(digits[2:])
        except ValueError:
            return None
    return None


def _format_time(raw: str) -> str:
    mins = _minutes_of_day(raw)
    if mins is None:
        return (raw or "").strip()
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _duration_minutes(dep: str, arr: str) -> int | None:
    a = _minutes_of_day(dep)
    b = _minutes_of_day(arr)
    if a is None or b is None:
        return None
    delta = b - a
    if delta < 0:
        delta += 24 * 60
    return delta


def _time_band(dep: str) -> str:
    mins = _minutes_of_day(dep)
    if mins is None:
        return "any"
    hour = mins // 60
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def _fare_rank(class_code: str) -> int:
    code = (class_code or "").strip().upper()
    try:
        return FARE_LADDER.index(code)
    except ValueError:
        return 50 + ord(code[:1] or "?")


def title_place(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    if text.isupper() or text.islower():
        return " ".join(part.capitalize() for part in text.replace("_", " ").split())
    return text


def _parse_availability_rows(payload: str) -> list[dict[str, str]]:
    if not payload.strip():
        return []
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise BookingClientError("Could not read flight results.") from exc

    rows: list[dict[str, str]] = []
    for direction_el in root.iter():
        direction = _local(direction_el.tag)
        if direction not in {"Outbound", "Inbound"}:
            continue
        for avail in direction_el:
            if _local(avail.tag) != "Availability":
                continue
            row = {name: _child_text(avail, name) for name in AVAIL_FIELDS}
            row["Direction"] = direction
            if any(row.get(k) for k in ("FlightNo", "FlightId", "AdultFare")):
                rows.append(row)
    if not rows:
        flat = parse_rows(payload, "Availability", AVAIL_FIELDS)
        for row in flat:
            row["Direction"] = "Outbound"
        rows = flat
    return rows


def group_flights(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for row in rows:
        flight_no = (row.get("FlightNo") or "").strip()
        airline = _airline_code(row.get("Airline") or "", flight_no)
        dep_raw = (row.get("DepartureTime") or "").strip()
        arr_raw = (row.get("ArrivalTime") or "").strip()
        dep = _format_time(dep_raw) or dep_raw
        arr = _format_time(arr_raw) or arr_raw
        direction = (row.get("Direction") or "Outbound").strip()
        origin = (row.get("Departure") or "").strip().upper()
        dest = (row.get("Arrival") or "").strip().upper()
        key = f"{direction}|{airline}|{flight_no}|{dep}|{origin}|{dest}"
        if key not in groups:
            meta = AIRLINE_META.get(airline, {"name": airline or "Airline", "color": "#334155"})
            duration = _duration_minutes(dep_raw or dep, arr_raw or arr)
            groups[key] = {
                "group_id": key,
                "direction": direction,
                "airline_code": airline,
                "airline_name": meta["name"],
                "airline_color": meta["color"],
                "flight_no": flight_no,
                "aircraft": (row.get("AircraftType") or "").strip(),
                "from": origin,
                "to": dest,
                "from_name": title_place(origin),
                "to_name": title_place(dest),
                "flight_date": (row.get("FlightDate") or "").strip(),
                "dep_time": dep,
                "arr_time": arr,
                "duration_min": duration,
                "time_band": _time_band(dep),
                "currency": (row.get("Currency") or "NPR").strip() or "NPR",
                "fares": [],
            }
            order.append(key)

        adult = _money_num(row.get("AdultFare") or "")
        child = _money_num(row.get("ChildFare") or "")
        fuel = _money_num(row.get("FuelSurcharge") or "") or 0.0
        tax = _money_num(row.get("Tax") or "") or 0.0
        vat = _money_num(row.get("AdultVAT") or "") or 0.0
        child_vat = _money_num(row.get("ChildVAT") or "") or 0.0
        class_code = (row.get("FlightClassCode") or "").strip().upper() or "?"
        total = None if adult is None else adult + fuel + tax + vat
        child_total = None if child is None else child + fuel + tax + child_vat
        refundable_raw = (row.get("Refundable") or "").strip().lower()
        if refundable_raw in {"y", "yes", "true", "1", "refundable"}:
            refundable = True
        elif refundable_raw in {"n", "no", "false", "0", "non-refundable", "nonrefundable"}:
            refundable = False
        else:
            refundable = None
        commission = _money_num(row.get("AgencyCommission") or "")
        baggage = (row.get("FreeBaggage") or "").strip()
        groups[key]["fares"].append(
            {
                "flight_id": (row.get("FlightId") or "").strip(),
                "class_code": class_code,
                "adult_fare": adult,
                "child_fare": child,
                "res_fare": _money_num(row.get("ResFare") or ""),
                "fuel_surcharge": fuel,
                "tax": tax,
                "vat": vat,
                "child_vat": child_vat,
                "total": total,
                "child_total": child_total,
                "refundable": refundable,
                "baggage": baggage or None,
                "agency_commission": commission,
                "child_commission": _money_num(row.get("ChildCommission") or ""),
                "currency": (row.get("Currency") or groups[key]["currency"]).strip() or "NPR",
            }
        )

    flights: list[dict[str, Any]] = []
    for key in order:
        g = groups[key]
        # Keep cheapest row per fare class (API may repeat classes).
        by_class: dict[str, dict[str, Any]] = {}
        for fare in g["fares"]:
            code = fare["class_code"]
            prev = by_class.get(code)
            if prev is None or (fare["total"] is not None and (
                prev["total"] is None or fare["total"] < prev["total"]
            )):
                by_class[code] = fare
        fares = list(by_class.values())
        fares.sort(key=lambda f: (_fare_rank(f["class_code"]), f["total"] is None, f["total"] or 0))
        g["fares"] = fares
        totals = [f["total"] for f in fares if f["total"] is not None]
        g["from_price"] = min(totals) if totals else None
        g["fare_count"] = len(fares)
        flights.append(g)

    flights.sort(
        key=lambda g: (
            0 if g["direction"] == "Outbound" else 1,
            g["dep_time"] or "99:99",
            g["from_price"] is None,
            g["from_price"] or 0,
        )
    )
    return flights


def _airline_summary(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bucket: dict[str, dict[str, Any]] = {}
    for flight in flights:
        code = flight["airline_code"]
        item = bucket.get(code)
        if item is None:
            item = {
                "code": code,
                "name": flight["airline_name"],
                "color": flight["airline_color"],
                "count": 0,
                "from_price": None,
            }
            bucket[code] = item
        item["count"] += 1
        price = flight.get("from_price")
        if price is not None and (item["from_price"] is None or price < item["from_price"]):
            item["from_price"] = price
    return sorted(bucket.values(), key=lambda a: (a["from_price"] is None, a["from_price"] or 0, a["name"]))


def _build_result(
    *,
    query: dict[str, Any],
    flights: list[dict[str, Any]],
    fare_rows: int,
    cached: bool,
) -> dict[str, Any]:
    outbound = [f for f in flights if f["direction"] != "Inbound"]
    inbound = [f for f in flights if f["direction"] == "Inbound"]
    prices = [f["from_price"] for f in flights if f.get("from_price") is not None]
    return {
        "query": query,
        "count": len(flights),
        "flights": flights,
        "outbound": outbound,
        "inbound": inbound,
        "airlines": _airline_summary(flights),
        "meta": {
            "source": "live",
            "cached": cached,
            "fare_rows": fare_rows,
            "outbound_count": len(outbound),
            "inbound_count": len(inbound),
            "airline_count": len({f["airline_code"] for f in flights if f.get("airline_code")}),
            "cheapest": min(prices) if prices else None,
            "currency": next((f.get("currency") for f in flights if f.get("currency")), "NPR"),
        },
    }


def search_availability(
    *,
    sector_from: str,
    sector_to: str,
    flight_date: str,
    trip_type: str = "O",
    return_date: str = "",
    nationality: str = "NP",
    adults: int = 1,
    children: int = 0,
) -> dict[str, Any]:
    sector_from = (sector_from or "").strip().upper()
    sector_to = (sector_to or "").strip().upper()
    if not sector_from or not sector_to:
        raise BookingClientError("Choose from and to cities.")
    if sector_from == sector_to:
        raise BookingClientError("From and to cities must be different.")

    api_date = to_api_date(flight_date) if flight_date else ""
    if not api_date:
        api_date = (date.today() + timedelta(days=7)).strftime("%d-%b-%Y").upper()

    trip = (trip_type or "O").strip().upper()
    if trip not in {"O", "R"}:
        trip = "O"
    ret = to_api_date(return_date) if return_date and trip == "R" else ""
    if trip == "R" and not ret:
        raise BookingClientError("Pick a return date for round-trip search.")

    adults = max(1, min(9, int(adults or 1)))
    children = max(0, min(9, int(children or 0)))
    nationality = (nationality or "NP").strip().upper()[:2] or "NP"

    query = {
        "from": sector_from,
        "to": sector_to,
        "flight_date": api_date,
        "return_date": ret,
        "trip_type": trip,
        "nationality": nationality,
        "adults": adults,
        "children": children,
    }

    cache_key = hashlib.sha1(
        json.dumps(query, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    now = time.time()
    if (
        _SEARCH_CACHE["result"] is not None
        and _SEARCH_CACHE["key"] == cache_key
        and now - float(_SEARCH_CACHE["at"]) < _SEARCH_TTL
    ):
        cached = dict(_SEARCH_CACHE["result"])
        cached["meta"] = {**cached.get("meta", {}), "cached": True}
        return cached

    creds = load_booking_creds()
    if not creds.password or not creds.agency_id:
        raise BookingClientError(
            "Flight search credentials must be set in Settings before public search can run."
        )

    fields = {
        "strUserId": creds.user_id,
        "strPassword": creds.password,
        "strAgencyId": creds.agency_id,
        "strSectorFrom": sector_from,
        "strSectorTo": sector_to,
        "strFlightDate": api_date,
        "strReturnDate": ret,
        "strTripType": trip,
        "strNationality": nationality,
        "intAdult": str(adults),
        "intChild": str(children),
        "strClientIP": creds.client_ip or creds.user_id,
    }
    raw = soap_call(creds.endpoint, "FlightAvailability", fields, creds.timeout)
    payload = extract_return_xml(raw)
    rows = _parse_availability_rows(payload)
    flights = group_flights(rows)
    result = _build_result(query=query, flights=flights, fare_rows=len(rows), cached=False)
    _SEARCH_CACHE["key"] = cache_key
    _SEARCH_CACHE["at"] = now
    _SEARCH_CACHE["result"] = result
    return result
