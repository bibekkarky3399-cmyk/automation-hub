#!/usr/bin/env python3
"""
Yeti Airlines B2B Group Booking Automation (route-agnostic) — PNR workflow.

Books a specific flight number + fare class for every day in a date range.
Configured from the Helix via config/scripts.json, or CLI:

  python scripts/pnr/main.py \\
    --origin KTM --destination BIR --flight-number 781 --fare-code E1 \\
    --start-date 2026-08-02 --end-date 2026-08-31 \\
    --agency-name mmt --user-login USER --password PASS \\
    --output ./outputs

Credentials: pass --password, or set env PNR_PASSWORD (never commit secrets).

Dependencies:
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

PORTAL_URL = "https://res.yetiairlines.com/b2b/User.aspx"

# Valid airport codes on this portal:
#   BDP BHADRAPUR | BWA BHAIRAHAWA | BIR BIRATNAGAR | JKR JANAKPUR
#   KTM KATHMANDU | KEP NEPALGUNJ  | PKR POKHARA     | TPU TIKAPUR (dest only)

PNR_RE = re.compile(r"\bPNR\s*[:#]?\s*([A-Z0-9]{5,8})\b", re.IGNORECASE)


@dataclass
class Config:
    origin: str
    destination: str
    flight_number: str
    fare_code: str
    fare_price: float
    start_date: str
    end_date: str
    agency_name: str
    user_login: str
    password: str
    adults: int
    currency: str
    group_name: str
    output: str
    headless: bool
    keep_browser_open: bool


@dataclass
class DateResult:
    day: str
    year_month: str
    status: str  # booked | failed
    reason: str
    pnr: str = ""


CFG: Config | None = None


def parse_flexible_date(value: str) -> date:
    """Accept YYYY-MM-DD (preferred) or DD-MMM-YYYY (legacy Hub/API style)."""
    text = (value or "").strip()
    if not text:
        raise ValueError("Empty date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"Invalid date '{value}'. Use YYYY-MM-DD (e.g. 2026-08-02)."
    )


def days_in_range(start_str: str, end_str: str) -> list[tuple[str, str]]:
    """Return (day, year_month) tuples for every date from start to end inclusive."""
    start = parse_flexible_date(start_str)
    end = parse_flexible_date(end_str)
    if end < start:
        raise ValueError(f"end-date {end_str} is before start-date {start_str}")
    dates: list[tuple[str, str]] = []
    current = start
    while current <= end:
        day = f"{current.day:02d}"
        ym = f"{current.year}{current.month:02d}"
        dates.append((day, ym))
        current += timedelta(days=1)
    return dates


async def login(page) -> bool:
    assert CFG is not None
    print("\n🔐 Logging in...")
    await page.goto(PORTAL_URL)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_selector(".PleaseLogin", timeout=10000)

    await page.fill("input#txtCompany", CFG.agency_name)
    await page.fill("input#txtAdminName", CFG.user_login)
    await page.fill("input#txtPassword", CFG.password)
    await page.click("a[title='User Login']")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    try:
        await page.wait_for_selector("text=Account Information", timeout=5000)
        print("  ✅ Login successful")
        return True
    except Exception as e:
        print(f"  ⚠️ Could not confirm login: {e}")
        try:
            error = await page.locator("#pnError").first.text_content()
            print(f"  ❌ Login failed: {error}")
        except Exception:
            print("  ❌ Login failed")
        return False


async def go_home(page) -> None:
    """Reset to the search page between dates."""
    try:
        await page.click("text=Home")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
    except Exception as e:
        print(f"  ⚠️ Could not click Home, falling back to goto(): {e}")
        await page.goto(PORTAL_URL)
        await page.wait_for_load_state("networkidle")


async def search_flights(page, origin: str, dest: str, day: str, year_month: str) -> bool:
    assert CFG is not None
    print(f"\n🔍 Searching {origin}-{dest} on {day}/{year_month}...")

    await page.wait_for_selector("#SearchBox", timeout=10000)
    await page.click("input#rd2[name='rd1']")  # Oneway

    print(f"  → Setting origin = {origin}")
    await page.select_option("select#uxOrigin", value=origin)
    await page.evaluate(
        "() => document.getElementById('uxOrigin')"
        ".dispatchEvent(new Event('change', { bubbles: true }))"
    )
    await asyncio.sleep(1.5)
    origin_now = await page.evaluate("() => document.getElementById('uxOrigin').value")
    if origin_now != origin:
        print(f"  ❌ Origin didn't stick (wanted {origin}, got {origin_now})")
        return False
    print(f"  ✓ Origin set to {origin_now}")

    print(f"  → Setting destination = {dest}")
    dest_available = await page.evaluate(
        "() => Array.from(document.getElementById('uxDest').options)"
        f".some(o => o.value === '{dest}')"
    )
    if not dest_available:
        avail = await page.evaluate(
            "() => Array.from(document.getElementById('uxDest').options).map(o => o.value)"
        )
        print(f"  ❌ Destination {dest} not available from {origin}. Valid: {avail}")
        return False
    await page.select_option("select#uxDest", value=dest)
    await page.evaluate(
        "() => document.getElementById('uxDest')"
        ".dispatchEvent(new Event('change', { bubbles: true }))"
    )
    await asyncio.sleep(0.5)
    dest_now = await page.evaluate("() => document.getElementById('uxDest').value")
    if dest_now != dest:
        print(f"  ❌ Destination didn't stick (wanted {dest}, got {dest_now})")
        return False
    print(f"  ✓ Destination set to {dest_now}")

    await page.select_option("select#ddlDate_1", value=day)
    await page.select_option("select#ddlMY_1", value=year_month)

    await page.check("input#uxCHKGroup")
    await page.evaluate(
        """
        () => {
            const g = document.getElementById('uxCHKGroup');
            if (!g.checked) g.checked = true;
            creategroup(g);
        }
        """
    )
    await asyncio.sleep(1)
    await page.select_option("select#optAdult", value=str(CFG.adults))
    await page.select_option("select#optCurrency", value=CFG.currency)

    adult_now = await page.evaluate("() => document.getElementById('optAdult').value")
    if adult_now != str(CFG.adults):
        print(f"  ❌ Adults didn't stick (wanted {CFG.adults}, got {adult_now})")
        return False
    print(f"  ✓ Adults set to {adult_now}")

    try:
        await page.evaluate("ValidateFlightAvailability();")
    except Exception as e:
        print(f"  ⚠️ JS call failed: {e}")
        try:
            await page.click("a:has-text('Next')")
        except Exception as e2:
            print(f"  ❌ Could not submit search: {e2}")
            return False

    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(3)

    try:
        await page.wait_for_selector("#tabOutward", timeout=10000)
        print("  ✅ Results loaded")
        return True
    except Exception as e:
        print(f"  ⚠️ No results table: {e}")
        return False


async def select_flight(page) -> bool:
    assert CFG is not None
    try:
        await page.wait_for_selector("#tabOutward", timeout=10000)
        await asyncio.sleep(2)
    except Exception as e:
        print(f"  ❌ Results table not found: {e}")
        return False

    rows = await page.evaluate(
        r"""
        () => {
            const results = [];
            document.querySelectorAll('#tabOutward tr').forEach(row => {
                const radio = row.querySelector('input[name="Rad_Out"]');
                if (!radio) return;
                const tds = row.querySelectorAll('td');
                if (tds.length < 9) return;
                const flightCell = tds[0] ? tds[0].textContent.trim() : '';
                const fareCell   = tds[6] ? tds[6].textContent.trim() : '';
                const seatCell   = tds[7] ? tds[7].textContent.trim() : '';
                const priceCell  = tds[8] ? tds[8].textContent.trim() : '';
                let flightNumber = '';
                const m = flightCell.match(/YT\s*(\d{3})/);
                if (m) flightNumber = m[1];
                let price = 0;
                if (priceCell) price = parseFloat(priceCell.replace(/,/g, '')) || 0;
                let seats = 0;
                const sm = seatCell.match(/(\d+)/);
                if (sm) seats = parseInt(sm[1], 10);
                results.push({ radioValue: radio.value, flightNumber, fareCode: fareCell,
                               price, priceStr: priceCell, seats });
            });
            return results;
        }
        """
    )

    if not rows:
        print("  ❌ No fare rows found")
        return False

    matches = [
        r
        for r in rows
        if r["flightNumber"] == CFG.flight_number and r["fareCode"] == CFG.fare_code
    ]
    if not matches:
        print(
            f"  ❌ No row matches Flight {CFG.flight_number} / Fare {CFG.fare_code}. Available:"
        )
        for r in rows:
            print(
                f"     - YT {r['flightNumber'] or '???':<4} {r['fareCode']:<4} "
                f"NPR {r['priceStr']:<12} seats={r['seats']}"
            )
        return False

    chosen = matches[0]
    if CFG.fare_price and abs(chosen["price"] - CFG.fare_price) > 0.01:
        print(
            f"  ⚠️ Price mismatch (config {CFG.fare_price}, site {chosen['priceStr']}). Proceeding."
        )
    if chosen["seats"] < CFG.adults:
        print(f"  ❌ Only {chosen['seats']} seats, need {CFG.adults}. Skipping.")
        return False

    print(
        f"  ✅ Match: YT {chosen['flightNumber']} / {chosen['fareCode']} "
        f"/ NPR {chosen['priceStr']} / seats={chosen['seats']}"
    )

    try:
        await page.check(f'input[name="Rad_Out"][value="{chosen["radioValue"]}"]')
    except Exception as e:
        print(f"  ❌ Failed to select radio: {e}")
        return False

    if not await click_next(page):
        print("  ❌ Could not click Next after selecting flight")
        return False
    await asyncio.sleep(2)
    return True


async def click_next(page) -> bool:
    """Try several ways to click Next. Returns True if something was clicked."""
    btn = await page.query_selector("#btnNextSaveStep a")
    if btn:
        try:
            await btn.click()
            await page.wait_for_load_state("networkidle")
            return True
        except Exception:
            pass
    for sel in ["a:has-text('Next')", "text=Next", "input[value='Next']"]:
        try:
            await page.click(sel, timeout=2500)
            await page.wait_for_load_state("networkidle")
            return True
        except Exception:
            continue
    return False


async def try_fill_group_name(page) -> bool:
    assert CFG is not None
    candidate_selectors = [
        "input#uxGroupName",
        "input#txtGroupName",
        "input[name='GroupName']",
        "input[name='groupname']",
        "input[id*='Group']",
        "input[id*='group']",
    ]
    for sel in candidate_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                visible = await el.is_visible()
                if visible:
                    await el.fill(CFG.group_name)
                    print(f"    → Filled group name '{CFG.group_name}' into {sel}")
                    return True
        except Exception:
            continue
    return False


async def read_page_state(page) -> dict:
    return await page.evaluate(
        """
        () => {
            const headEl = document.querySelector('.TicketText, .StepText, h1, h2, .Label');
            const heading = headEl ? headEl.textContent.trim() : '';
            const body = (document.body.innerText || '');
            return { heading, body, preview: body.slice(0, 600) };
        }
        """
    )


def extract_pnr(text: str) -> str:
    match = PNR_RE.search(text or "")
    return match.group(1).upper() if match else ""


async def advance_through_booking(page, max_steps: int = 10) -> tuple[bool, str]:
    """Return (ok, pnr_or_empty)."""
    seen_headings: list[str] = []
    for step in range(max_steps):
        await asyncio.sleep(2)
        state = await read_page_state(page)
        heading, body = state["heading"], state["body"]
        preview = state.get("preview") or body[:600]
        seen_headings.append(heading)
        print(f"    [advance {step+1}] heading: {heading!r}")

        if (
            "Booking Details" in body
            or "PNR" in body
            or "Please pay within" in body
            or "Confirmed" in body
        ):
            pnr = extract_pnr(body)
            if pnr:
                print(f"  ✅ Booking confirmed! PNR={pnr}")
            else:
                print("  ✅ Booking confirmed!")
            return True, pnr

        if "Book Now Pay Later" in body or "Payment" in heading or "Payment" in body[:200]:
            print("    → Payment: selecting Book Now Pay Later")
            for sel in [
                "text=Book Now Pay Later",
                "div:has-text('Book Now Pay Later')",
            ]:
                try:
                    await page.click(sel, timeout=2500)
                    break
                except Exception:
                    continue
            await asyncio.sleep(1)
            await click_next(page)
            continue

        if "Special Service" in body or "Special Service" in heading:
            print("    → Special Service: skipping")
            await click_next(page)
            continue

        if (
            "Your Details" in body
            or "Contact Person" in body
            or "Group Name" in body
            or "Your Itinerary" in heading
        ):
            print("    → Details/Itinerary page")
            filled = await try_fill_group_name(page)
            if not filled:
                print(
                    "    → No editable group-name field here "
                    "(auto-filled from profile); clicking Next"
                )
            await click_next(page)
            continue

        if "Error" in body or "Sorry" in body:
            print("    ❌ Error page detected. Body start:")
            print("      " + preview.replace("\n", " ")[:250])
            try:
                err = await page.locator(".ErrorBox, #pnError, .TextRed").first.text_content()
                if err and err.strip():
                    print(f"      📝 {err.strip()[:200]}")
            except Exception:
                pass
            return False, ""

        print(f"    ⚠️ Unrecognized page (step {step+1}). Heading={heading!r}")
        print("      Body start: " + preview.replace("\n", " ")[:250])
        await try_fill_group_name(page)
        if not await click_next(page):
            print("    ❌ No Next button on this page — cannot proceed.")
            print(f"      Pages seen so far: {seen_headings}")
            return False, ""

    print(f"  ❌ Exceeded {max_steps} steps without confirmation. Seen: {seen_headings}")
    return False, ""


async def process_one_date(page, origin: str, dest: str, day: str, year_month: str):
    if not await search_flights(page, origin, dest, day, year_month):
        return False, "search failed", ""
    if not await select_flight(page):
        return False, "flight not matched / not selected", ""
    ok, pnr = await advance_through_booking(page)
    if not ok:
        return False, "could not complete booking flow", ""
    return True, "booked", pnr


async def process_one_date_with_retry(
    page, origin: str, dest: str, day: str, year_month: str, attempts: int = 2
):
    last_reason = "unknown"
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(
                f"  🔁 Retry {attempt}/{attempts} for {day}/{year_month} "
                f"(previous: {last_reason})"
            )
            await go_home(page)
            await asyncio.sleep(1)
        ok, reason, pnr = await process_one_date(page, origin, dest, day, year_month)
        if ok:
            return True, reason, pnr
        last_reason = reason
        if reason == "flight not matched / not selected":
            break
    return False, last_reason, ""


def write_results_csv(cfg: Config, rows: list[DateResult]) -> Path:
    out_dir = Path(cfg.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uuid.uuid4()}.csv"
    print(f"\n📝 Writing CSV → {path}")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "date_label",
                "day",
                "year_month",
                "origin",
                "destination",
                "flight_number",
                "fare_code",
                "adults",
                "currency",
                "group_name",
                "status",
                "pnr",
                "reason",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "date_label": f"{row.day}/{row.year_month}",
                    "day": row.day,
                    "year_month": row.year_month,
                    "origin": cfg.origin,
                    "destination": cfg.destination,
                    "flight_number": cfg.flight_number,
                    "fare_code": cfg.fare_code,
                    "adults": cfg.adults,
                    "currency": cfg.currency,
                    "group_name": cfg.group_name,
                    "status": row.status,
                    "pnr": row.pnr,
                    "reason": row.reason,
                }
            )
    print(f"CSV: {path}")
    print(f"rows_total={len(rows)}")
    return path


def _browser_launch_kwargs(cfg: Config) -> dict:
    """Launch Chromium headless without a visible window, with fewer bot tells."""
    kwargs: dict = {
        "headless": cfg.headless,
        # slow_mo is only useful when watching a headed window
        "slow_mo": 0 if cfg.headless else 200,
    }
    if cfg.headless:
        kwargs["args"] = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    return kwargs


async def run_booking(cfg: Config) -> int:
    global CFG
    CFG = cfg

    origin = cfg.origin.strip().upper()
    dest = cfg.destination.strip().upper()
    date_list = days_in_range(cfg.start_date, cfg.end_date)

    print("\n" + "=" * 60)
    print("🚀 YETI AIRLINES B2B AUTOMATION — Batch Booking (PNR)")
    print(
        f"   Sector: {origin}-{dest} | Flight: YT{cfg.flight_number} | "
        f"Fare: {cfg.fare_code} ({cfg.fare_price})"
    )
    print(
        f"   Adults: {cfg.adults} | Range: {cfg.start_date} → {cfg.end_date} "
        f"({len(date_list)} dates)"
    )
    if cfg.headless:
        print("   Browser: headless (no visible window)")
    else:
        print("   Browser: headed (visible Chromium window)")
    print("=" * 60)

    rows: list[DateResult] = []
    exit_code = 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(**_browser_launch_kwargs(cfg))
        # Context lets us drop the "HeadlessChrome" UA that some portals treat differently.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()
        if cfg.headless:
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

        try:
            if not await login(page):
                print("\n❌ Stopped — login failed")
                print("SUMMARY: booked=0 failed=login")
                # No bookings attempted — skip empty CSV so Hub stage stays on Login.
                return 1

            for i, (day, year_month) in enumerate(date_list):
                print("\n" + "-" * 60)
                if i == 0:
                    print(f"📅 Date: {day}/{year_month}  🧪 TEST DATE (first date)")
                else:
                    print(f"📅 Date: {day}/{year_month}")
                print("-" * 60)

                ok, reason, pnr = await process_one_date_with_retry(
                    page, origin, dest, day, year_month
                )

                if ok:
                    rows.append(
                        DateResult(
                            day=day,
                            year_month=year_month,
                            status="booked",
                            reason=reason,
                            pnr=pnr,
                        )
                    )
                    if i == 0:
                        print(
                            f"  ✅ Test date PASSED — booking counted, "
                            f"continuing with remaining {len(date_list) - 1} dates..."
                        )
                else:
                    print(f"  ❌ {reason}")
                    rows.append(
                        DateResult(
                            day=day,
                            year_month=year_month,
                            status="failed",
                            reason=reason,
                            pnr="",
                        )
                    )
                    if i == 0:
                        print(
                            "  🛑 Test date FAILED — stopping. "
                            "Fix the issue before running the full range."
                        )
                        break

                await go_home(page)

            booked = [r for r in rows if r.status == "booked"]
            failed = [r for r in rows if r.status == "failed"]

            print("\n" + "=" * 60)
            print("📊 SUMMARY")
            print("=" * 60)
            print(f"✅ Booked: {len(booked)}")
            for r in booked:
                pnr_bit = f" PNR={r.pnr}" if r.pnr else ""
                print(f"   - {r.day}/{r.year_month}{pnr_bit}")
            print(f"❌ Failed: {len(failed)}")
            for r in failed:
                print(f"   - {r.day}/{r.year_month} ({r.reason})")
            print("=" * 60)
            print(f"SUMMARY: booked={len(booked)} failed={len(failed)}")

            write_results_csv(cfg, rows)

            # Partial success still exits 0 so Hub marks job success; failures are in log.
            exit_code = 0 if booked else 1

            if cfg.keep_browser_open and not cfg.headless:
                print("\n🟢 Browser staying open — close manually or press Ctrl+C")
                try:
                    await page.wait_for_event("close", timeout=0)
                except Exception:
                    pass

        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted by user")
            exit_code = 130
            try:
                write_results_csv(cfg, rows)
            except Exception:
                pass
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback

            traceback.print_exc()
            exit_code = 1
            try:
                write_results_csv(cfg, rows)
            except Exception:
                pass
        finally:
            await context.close()
            await browser.close()
            print("\n🔴 Done.")

    return exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Yeti B2B group booking (PNR) automation")
    p.add_argument("--origin", required=True, help="Origin airport code, e.g. KTM")
    p.add_argument("--destination", required=True, help="Destination code, e.g. BIR")
    p.add_argument("--flight-number", required=True, help="Flight number without YT, e.g. 781")
    p.add_argument("--fare-code", required=True, help="Fare class code, e.g. E1")
    p.add_argument("--fare-price", type=float, default=0, help="Expected fare (sanity check)")
    p.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--agency-name", required=True, help="B2B agency / company name")
    p.add_argument("--user-login", required=True, help="Portal username")
    p.add_argument(
        "--password",
        default="",
        help="Portal password (or set env PNR_PASSWORD)",
    )
    p.add_argument("--adults", type=int, default=10)
    p.add_argument("--currency", default="NPR")
    p.add_argument("--group-name", default="CDV")
    p.add_argument(
        "--output",
        required=True,
        help="Output folder for booking results CSV",
    )
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run Chromium headless (default: true for Hub)",
    )
    p.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Leave browser open after run (headed mode only)",
    )
    return p


def parse_config(argv: list[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)
    password = (args.password or os.environ.get("PNR_PASSWORD") or "").strip()
    if not password:
        raise SystemExit(
            "Missing password: pass --password or set environment variable PNR_PASSWORD"
        )
    return Config(
        origin=args.origin.strip().upper(),
        destination=args.destination.strip().upper(),
        flight_number=args.flight_number.strip(),
        fare_code=args.fare_code.strip(),
        fare_price=float(args.fare_price or 0),
        start_date=args.start_date.strip(),
        end_date=args.end_date.strip(),
        agency_name=args.agency_name.strip(),
        user_login=args.user_login.strip(),
        password=password,
        adults=int(args.adults),
        currency=args.currency.strip().upper(),
        group_name=args.group_name.strip(),
        output=args.output.strip(),
        headless=bool(args.headless),
        keep_browser_open=bool(args.keep_browser_open),
    )


def main(argv: list[str] | None = None) -> int:
    cfg = parse_config(argv)
    try:
        return asyncio.run(run_booking(cfg))
    except KeyboardInterrupt:
        print("\n\n👋 Exited by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
