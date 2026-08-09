#!/usr/bin/env python3
"""
Book a Yeti PNR via B2B ASMX APIs (no Chromium).

Uses the same ScriptServices the portal HTML calls:
  UserLogOn → getFlightAvailabilityFormMultiCurrency → GetSelectFlight
  → SavePassengerDetail / savestep4 → FillContactDetail → Paylater

This script imports the client from the aggregator project when available.

Example:

  export PNR_PASSWORD='...'
  python scripts/pnr/yeti_api_book.py \\
    --agency-name mmt --user-login USER \\
    --origin KTM --destination PKR \\
    --departure-date 2026-09-15 \\
    --flight-number 673 --fare-code E1 \\
    --adults 1 --group-name DEMO
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Prefer aggregator client (shared implementation)
AGG = Path(__file__).resolve().parents[3] / "aggregator"
if AGG.exists() and str(AGG) not in sys.path:
    sys.path.insert(0, str(AGG))

try:
    from app.adapters.yeti_b2b_client import (  # type: ignore
        YetiB2BClient,
        YetiContact,
        YetiCredentials,
        YetiPassenger,
    )
except ImportError as exc:  # pragma: no cover
    print(
        "Cannot import aggregator Yeti client. "
        f"Expected sibling repo at {AGG}. ({exc})",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


async def amain() -> int:
    p = argparse.ArgumentParser(description="Yeti B2B API PNR (no Chromium)")
    p.add_argument("--agency-name", required=True)
    p.add_argument("--user-login", required=True)
    p.add_argument("--password", default="")
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--departure-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--flight-number", required=True)
    p.add_argument("--fare-code", default="")
    p.add_argument("--flight-fare-id", default="")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="NPR")
    p.add_argument("--group-name", default="API")
    p.add_argument("--contact-name", default="Demo Agent")
    p.add_argument("--contact-mobile", default="9800000000")
    p.add_argument("--contact-email", default="")
    p.add_argument("--search-only", action="store_true")
    args = p.parse_args()

    password = (args.password or os.environ.get("PNR_PASSWORD") or "").strip()
    if not password:
        print("Missing password (--password or PNR_PASSWORD)", file=sys.stderr)
        return 2

    creds = YetiCredentials(
        agency_name=args.agency_name,
        user_login=args.user_login,
        password=password,
    )
    contact = YetiContact(
        contact_person=args.contact_name,
        mobile_phone=args.contact_mobile,
        email=args.contact_email,
        group_name=args.group_name,
    )

    async with YetiB2BClient(creds) as client:
        print("🔐 Login…")
        await client.login()
        print("🔍 Availability…")
        fares = await client.search_availability(
            origin=args.origin,
            destination=args.destination,
            departure_date=args.departure_date,
            adults=args.adults,
            currency=args.currency,
            group=True,
        )
        print(f"  {len(fares)} fare rows")
        for f in fares[:15]:
            print(
                f"   - YT{f.flight_number} {f.fare_code:<4} "
                f"dep={f.departure_time or '—'} price={f.price} id={f.flight_fare_id}"
            )
        if args.search_only:
            return 0

        print("🎫 Creating PNR via Paylater…")
        result = await client.create_pnr(
            origin=args.origin,
            destination=args.destination,
            departure_date=args.departure_date,
            flight_number=args.flight_number,
            fare_code=args.fare_code,
            adults=args.adults,
            contact=contact,
            passengers=[
                YetiPassenger(first_name="GROUP", last_name="PAX")
                for _ in range(max(args.adults, 1))
            ],
            currency=args.currency,
            group=True,
            flight_fare_id=args.flight_fare_id or None,
        )
        print(json.dumps(
            {
                "success": result.success,
                "pnr": result.pnr,
                "booking_id": result.booking_id,
                "message": result.message,
                "selected": result.selected_fare.__dict__ if result.selected_fare else None,
            },
            indent=2,
        ))
        return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
