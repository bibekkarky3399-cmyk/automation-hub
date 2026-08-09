# Helix Flights — Live Nepal Domestic Search (United Solutions)

A reading guide for students and engineers who want to understand how Helix exposes **live multi-airline flight search** without login, using the **United Solutions Booking API** (SOAP) as the inventory source.

---

## 1. What problem this solves

Travel desks in Nepal often need to compare **Buddha Air, Yeti Airlines, Shree, Saurya**, and other domestic carriers on one screen. Those airlines largely sit on a shared reservation platform: **United Solutions**.

Helix already had a **CSV collector job** (`scripts/booking_api/main.py`) that called the same SOAP API for ops/reporting. This feature reuses that capability for a **public, agency-style search UI**:

| Goal | Approach |
|---|---|
| No login for searchers | Public Flask routes outside the auth gate |
| Multi-airline results | One `FlightAvailability` call already returns many carriers |
| Credentials stay secret | Password / agency id loaded server-side from Settings |
| Honest UI | Show API fields; do not invent fare names like “Economy” |

**Public URL:** `/flights`  
**JSON APIs:** `GET /api/flights/sectors`, `POST /api/flights/search`

---

## 2. Big picture architecture

```text
Browser (/flights)
    │
    │  GET  /api/flights/sectors
    │  POST /api/flights/search   { from, to, date, … }
    ▼
Flask (app.py)  ──public endpoints──►  no login required
    │
    ▼
launcher/booking_client.py
    │  load creds from config/scripts.json (booking_api)
    │  SOAP over HTTP
    ▼
United Solutions Booking API
    SectorCode  /  FlightAvailability
    │
    ▼
XML → parse → group by flight → JSON for UI
```

### Layers

1. **Presentation** — `templates/flights.html`, `static/css/flights.css`, `static/js/flights.js`  
   Standalone page (not the dark Helix desk theme). Search form, filters, flight cards.

2. **HTTP API** — `app.py`  
   Thin JSON wrappers that call the booking client and return `{ ok, … }`.

3. **Domain client** — `launcher/booking_client.py`  
   SOAP, XML parsing, fare grouping, caches, normalized JSON shape.

4. **Upstream CRS** — United Solutions  
   Source of cities, schedules, and fare rows.

5. **Related ops job** — `scripts/booking_api/main.py`  
   Same SOAP operations for CSV export (availability, sectors, balance, sales). Not required for the public UI, but shares concepts and field names.

---

## 3. Why United Solutions is “already an aggregator”

You do **not** call Buddha and Yeti separately for this search.

`FlightAvailability` accepts **route + date + passengers + nationality**. It does **not** take an airline filter. The response can contain many `<Availability>` rows under:

- `Outbound` — departing leg  
- `Inbound` — return leg (round trip)

Each row includes an `Airline` code (`U4`, `YT`, `SHA`, `S1`, …). Helix **groups** those rows into flights and fare chips for display.

So:

- **Upstream aggregation** = United Solutions across airlines  
- **Helix aggregation** = parse, group, sort, filter for the UI  

Missing an airline usually means agency rights or that carrier is not on the CRS — not a missing second API.

---

## 4. Authentication model (important for students)

### Public vs private

Helix normally requires login. Flight search is registered as **public**:

```python
# app.py (conceptually)
_PUBLIC_ENDPOINTS = frozenset({
    …,
    "flights_page",
    "api_flights_sectors",
    "api_flights_search",
})
```

Anyone who can reach the Helix host can open `/flights` and search.

### Booking credentials stay on the server

The browser never receives the SOAP password. Credentials are read from the Helix workflow config for script id `booking_api` in `config/scripts.json` (editable in **Settings**):

| Field | Role |
|---|---|
| `endpoint` | SOAP URL (dev default often `http://dev.usbooking.org/us/UnitedSolutions`) |
| `user_id` | API user |
| `password` | API password (required for availability) |
| `agency_id` | Agency identifier (required for availability) |
| `client_ip` | Client / agent marker sent as `strClientIP` |
| `timeout` | HTTP timeout seconds |

`SectorCode` needs the user id. `FlightAvailability` needs user + password + agency id.

**Security takeaway:** Public search is convenient, but it means **your agency’s live inventory** is exposed through Helix. Protect the host (VPN, tunnel ACL, etc.) the same way you would any B2B tool.

---

## 5. SOAP operations used

United Solutions is a classic **SOAP** service:

- Content-Type: `text/xml; charset=utf-8`
- `SOAPAction`: `"http://booking.us.org/{OperationName}"`
- Response payload is often nested inside `<return>…</return>` and may be HTML-escaped XML

Helix helper: `soap_call(endpoint, operation, fields, timeout)` in `booking_client.py`.

### 5.1 `SectorCode`

**Purpose:** List airports / cities (sectors).

**Request (simplified):**

```xml
<SectorCode>
  <strUserId>…</strUserId>
</SectorCode>
```

**Useful response fields:**

| Field | Meaning |
|---|---|
| `SectorCode` | Short code, e.g. `KTM`, `PKR` |
| `SectorName` | Name, e.g. `KATHMANDU` |

Helix normalizes to:

```json
{ "code": "KTM", "name": "Kathmandu" }
```

Names are title-cased for readability; the **code and name still come from the API**.

**Cache:** 6 hours in process memory (`_SECTOR_CACHE`).

### 5.2 `FlightAvailability`

**Purpose:** Live schedules and fare classes for a search.

**Request fields Helix sends:**

| SOAP field | Meaning |
|---|---|
| `strUserId` | User |
| `strPassword` | Password |
| `strAgencyId` | Agency |
| `strSectorFrom` / `strSectorTo` | Origin / destination codes |
| `strFlightDate` | Depart date as `DD-MMM-YYYY` (e.g. `12-AUG-2026`) |
| `strReturnDate` | Return date (round trip) or empty |
| `strTripType` | `O` one way, `R` return |
| `strNationality` | ISO-like 2-letter code, e.g. `NP`, `IN` |
| `intAdult` / `intChild` | Passenger counts |
| `strClientIP` | Client marker |

**Availability row fields Helix reads** (see `AVAIL_FIELDS` in `booking_client.py`):

`Airline`, `FlightDate`, `FlightNo`, `Departure`, `DepartureTime`, `Arrival`, `ArrivalTime`, `AircraftType`, `FlightId`, `FlightClassCode`, `Currency`, `AdultFare`, `ChildFare`, `FuelSurcharge`, `Tax`, `AdultVAT`, `ChildVAT`, `Refundable`, `FreeBaggage`, `AgencyCommission`, …

**Cache:** identical search queries cached ~45 seconds (`_SEARCH_CACHE`) to avoid hammering the CRS on double-clicks / back-button.

---

## 6. From flat fare rows to “flight cards”

The CRS often returns **one XML row per fare class**, not one row per flight.

Example shape (conceptual):

```text
YT671 07:20 KTM→PKR  class G  …
YT671 07:20 KTM→PKR  class N  …
YT671 07:20 KTM→PKR  class Y  …
U4603 08:00 KTM→PKR  class Y  …
```

Helix `group_flights()`:

1. **Group key** ≈ direction + airline + flight number + dep time + from + to  
2. Attach each row as a **fare** under that flight  
3. **Deduplicate** by `FlightClassCode`, keeping the cheapest total for that class  
4. Compute **`from_price`** = minimum fare total on that flight  
5. Sort flights by direction, departure time, then price  

### Fare total formula (Helix)

For an adult fare row:

```text
total = AdultFare + FuelSurcharge + Tax + AdultVAT
```

(Only when `AdultFare` is present; missing base → no total.)

This matches how agencies usually quote “all-in” from these components. The UI can also show the components separately when present.

### What is API vs Helix-derived

| Shown in UI | Source |
|---|---|
| City list | API `SectorCode` |
| From / to names on cards | API `Departure` / `Arrival` (title-cased) |
| Airline code | API `Airline` |
| Airline display name / color | Helix static map `AIRLINE_META` (code → name/color for UX) |
| Times, aircraft, flight no. | API |
| Class code (`Y`, `E1`, `G`, …) | API `FlightClassCode` |
| Invented labels (“Economy”, “Promo”) | **Not used** — removed on purpose |
| Baggage | API `FreeBaggage` (hidden if empty) |
| Refundable | API `Refundable` only if clearly Y/N (hidden if blank) |
| Duration minutes | Helix: computed from dep/arr times |
| Morning / afternoon / evening | Helix: bucketed from dep time for filters |
| Popular route chips | Helix hardcoded list, filtered to sectors that exist |

**Design rule:** do not invent commercial meaning for booking classes. Show `Class Y`, not a guessed “Economy”.

---

## 7. Helix HTTP API contract

### `GET /api/flights/sectors`

Optional query: `?refresh=1` to bypass sector cache.

**Success:**

```json
{
  "ok": true,
  "count": 27,
  "sectors": [
    { "code": "KTM", "name": "Kathmandu" },
    { "code": "PKR", "name": "Pokhara" }
  ]
}
```

### `POST /api/flights/search`

**Body (JSON):**

```json
{
  "from": "KTM",
  "to": "PKR",
  "date": "2026-08-12",
  "trip_type": "O",
  "return_date": "",
  "nationality": "NP",
  "adults": 1,
  "children": 0
}
```

Aliases accepted: `sector_from` / `sector_to` / `flight_date`.

**Success (shape):**

```json
{
  "ok": true,
  "query": { "from": "KTM", "to": "PKR", "flight_date": "12-AUG-2026", "trip_type": "O", … },
  "count": 21,
  "flights": [ /* all directions */ ],
  "outbound": [ /* … */ ],
  "inbound": [ /* … */ ],
  "airlines": [
    { "code": "YT", "name": "Yeti Airlines", "count": 11, "from_price": 4500, "color": "…" }
  ],
  "meta": {
    "source": "live",
    "cached": false,
    "fare_rows": 50,
    "outbound_count": 21,
    "inbound_count": 0,
    "airline_count": 2,
    "cheapest": 4500,
    "currency": "NPR"
  }
}
```

Each flight object includes `group_id`, `direction`, `airline_code`, times, `fares[]`, `from_price`, `duration_min`, `time_band`, etc.

**Errors:** `{ "ok": false, "error": "…" }` with HTTP 400/500. Messages are written for end users (no vendor branding in the public UI).

---

## 8. Front-end behaviour

### Files

| File | Role |
|---|---|
| `templates/flights.html` | Markup, form, results shell |
| `static/css/flights.css` | Agency-style layout (sky gradient, cards, chips) |
| `static/js/flights.js` | Load sectors, search, filter, render |

Entry points also link from the **login** page (“Search flights”) so visitors can open search without signing in.

### Search form

- Trip type: one way `O` / return `R`  
- From / To selects (from sectors API)  
- Depart (+ return when round trip)  
- Adults / children  
- Nationality **dropdown** (sent as `strNationality`)  

Popular chips (`KTM→PKR`, etc.) are **shortcuts only**; they are not from the CRS.

### Results tools

- **Airlines** filter: “All airlines · N” uses **airline count**; each carrier chip shows **flight count**  
- **Time** filter: Any time / Morning / Afternoon / Evening (derived from departure hour)  
- **Outbound / Return** tabs when inbound results exist  
- Sort: cheapest, earliest departure, airline  
- Fare chips: `Class {code}` + price; expand breakdown from API money fields  

---

## 9. End-to-end request walkthrough

1. User opens `/flights`.  
2. JS calls `GET /api/flights/sectors` → fills From/To.  
3. User picks KTM → PKR, date, nationality `NP`, submits.  
4. JS `POST /api/flights/search` with JSON body.  
5. Flask validates loosely and calls `search_availability(…)`.  
6. Client loads creds, converts `2026-08-12` → `12-AUG-2026`, SOAP `FlightAvailability`.  
7. XML parsed; rows grouped; JSON returned.  
8. UI renders cards; user filters to Yeti + morning, etc.  

No seat hold, booking, or ticketing is performed by this feature today — **search only**.

---

## 10. Related code map

```text
app.py                          Public routes + _PUBLIC_ENDPOINTS
launcher/booking_client.py      SOAP client, parse, group, cache
templates/flights.html          Public page
static/js/flights.js            Client logic
static/css/flights.css          Styles
config/scripts.json             booking_api credential defaults
scripts/booking_api/main.py     CSV ops job (same SOAP ops)
udaan-flight-search.jsx         Earlier UI blueprint / mock (not the live page)
```

---

## 11. Running and verifying locally

1. Configure `booking_api` credentials in Helix Settings (user, password, agency id, endpoint).  
2. Start Helix as usual.  
3. Open `http://127.0.0.1:5050/flights` (port may differ in your setup).  
4. Smoke checks:

```bash
# Sectors
curl -s http://127.0.0.1:5050/api/flights/sectors | python -m json.tool | head

# Search (example)
curl -s -X POST http://127.0.0.1:5050/api/flights/search \
  -H 'Content-Type: application/json' \
  -d '{"from":"KTM","to":"PKR","date":"2026-08-12","trip_type":"O","adults":1,"children":0,"nationality":"NP"}' \
  | python -m json.tool | head
```

Or use Flask’s test client in a short Python snippet importing `app`.

---

## 12. Design decisions (exam-style “why”)

| Decision | Why |
|---|---|
| Public route for search | Product ask: searchable without login; separate URL from ops desk |
| Server-side SOAP only | Keep agency password off the browser; one trusted client |
| Reuse `scripts.json` creds | Same agency already configured for Booking API → CSV |
| Group fare rows into flights | CRS returns class-level rows; UI needs flight-level cards |
| No invented fare names | API has codes, not marketing labels; honesty > pretty guesses |
| Short search cache | CRS is slow; identical retries should not double-hit |
| Long sector cache | City list changes rarely |
| Filters computed in JS | Instant UX without re-querying SOAP |

---

## 13. What is intentionally out of scope (today)

- Reservation / seat hold / ticket issue (`Reservation`, payment, PNR)  
- Multi-provider fan-out (Khalti, Amadeus, etc.)  
- Showing United Solutions branding in the public UI  
- Popular routes driven by real traffic analytics  
- Per-airline balance display on the public page (`CheckBalance` exists in the CSV job)

Those can be later chapters on the same client.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **CRS** | Central Reservation System — here, United Solutions |
| **Sector** | Airport / city code used on domestic routes |
| **SOAP** | XML-over-HTTP RPC style used by the booking endpoint |
| **Flight class / booking class** | Inventory bucket code (`Y`, `G`, `E1`, …), not cabin marketing name |
| **Outbound / Inbound** | Departing vs return direction in availability XML |
| **Agency id** | B2B identity that controls which inventory you can sell |

---

## 15. Suggested student exercises

1. Trace one live search: log raw SOAP XML (temporarily) and match fields to a flight card.  
2. Add a unit test for `group_flights()` with a tiny fake XML fixture (no network).  
3. Extend the UI to show `FlightId` only in a “debug” toggle for agents.  
4. Design (don’t necessarily build) the next step: hold + issue, and list which SOAP ops you’d need.  
5. Explain why caching sectors for 6 hours is safe but caching availability for 6 hours would be wrong.

---

*Document describes the Helix Flights live search implementation as built in this repository. Upstream United Solutions behaviour can vary by environment (dev vs production) and agency entitlements.*
