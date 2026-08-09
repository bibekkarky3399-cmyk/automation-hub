#!/usr/bin/env python3
"""
Capture HTTP traffic while driving the Yeti B2B portal (login + optional search).

The Chromium PNR workflow posts to ASP.NET ASMX ScriptServices under:
  https://res.yetiairlines.com/b2b/WebService/*.asmx/<Method>

This script records request/response pairs so we can replace Playwright UI clicks
with direct API calls.

Usage (from automation-hub root):

  python scripts/pnr/inspect_network.py \\
    --agency-name mmt --user-login USER --password PASS \\
    --origin KTM --destination PKR --day 15 --year-month 202609 \\
    --output outputs/yeti_network

Env: PNR_PASSWORD can supply --password.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

PORTAL_URL = "https://res.yetiairlines.com/b2b/User.aspx"
INTERESTING = re.compile(
    r"(WebService/|\.asmx|User\.aspx|__doPostBack|ScriptResource|ScriptHandler)",
    re.I,
)
SKIP_EXT = re.compile(r"\.(css|png|jpe?g|gif|woff2?|ttf|ico|svg|map)(\?|$)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _truncate(text: str | None, limit: int = 8000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated ({len(text)} chars total)"


async def main() -> int:
    p = argparse.ArgumentParser(description="Inspect Yeti B2B network APIs")
    p.add_argument("--agency-name", required=True)
    p.add_argument("--user-login", required=True)
    p.add_argument("--password", default="")
    p.add_argument("--origin", default="KTM")
    p.add_argument("--destination", default="PKR")
    p.add_argument("--day", default="15")
    p.add_argument("--year-month", default="202609")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="NPR")
    p.add_argument("--output", default="outputs/yeti_network")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headed", action="store_true")
    p.add_argument(
        "--search-only",
        action="store_true",
        default=True,
        help="Login + availability search only (default; no booking)",
    )
    args = p.parse_args()
    password = (args.password or os.environ.get("PNR_PASSWORD") or "").strip()
    if not password:
        print("Missing password: pass --password or set PNR_PASSWORD", file=sys.stderr)
        return 2

    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now()
    events: list[dict] = []
    seq = 0

    headless = not args.headed

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def on_response(response):
            nonlocal seq
            try:
                req = response.request
                url = response.url
                if SKIP_EXT.search(url):
                    return
                if not INTERESTING.search(url) and req.resource_type not in {
                    "xhr",
                    "fetch",
                    "document",
                }:
                    # Still keep asmx / postbacks even if resource_type is other
                    if "/b2b/" not in url:
                        return
                    if req.method == "GET" and req.resource_type in {"script", "stylesheet", "image", "font"}:
                        return

                body = ""
                try:
                    body = await response.text()
                except Exception:
                    body = ""

                post = ""
                try:
                    post = req.post_data or ""
                except Exception:
                    post = ""

                headers = {}
                try:
                    headers = await req.all_headers()
                except Exception:
                    headers = dict(req.headers or {})

                seq += 1
                path = urlparse(url).path
                events.append(
                    {
                        "seq": seq,
                        "method": req.method,
                        "url": url,
                        "path": path,
                        "resource_type": req.resource_type,
                        "status": response.status,
                        "request_headers": {
                            k: v
                            for k, v in headers.items()
                            if k.lower()
                            in {
                                "content-type",
                                "content-length",
                                "cookie",
                                "x-requested-with",
                                "soapaction",
                                "referer",
                            }
                        },
                        "post_data": _truncate(post, 12000),
                        "response_preview": _truncate(body, 12000),
                    }
                )
                short = path
                if "/WebService/" in path:
                    short = path.split("/WebService/")[-1]
                print(f"  [{seq:03d}] {req.method} {response.status} {short}")
            except Exception as exc:
                print(f"  ⚠️ capture error: {exc}")

        page.on("response", on_response)

        print("🔐 Login…")
        await page.goto(PORTAL_URL)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_selector(".PleaseLogin", timeout=15000)
        await page.fill("input#txtCompany", args.agency_name)
        await page.fill("input#txtAdminName", args.user_login)
        await page.fill("input#txtPassword", password)
        await page.click("a[title='User Login']")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        try:
            await page.wait_for_selector("text=Account Information", timeout=8000)
            print("  ✅ Login OK")
        except Exception:
            print("  ❌ Login failed — dumping captured traffic anyway")
            raw = out_dir / f"yeti_network_{stamp}.json"
            raw.write_text(json.dumps(events, indent=2), encoding="utf-8")
            print(f"Wrote {raw}")
            await browser.close()
            return 1

        print("🔍 Availability search…")
        await page.wait_for_selector("#SearchBox", timeout=10000)
        await page.click("input#rd2[name='rd1']")
        await page.select_option("select#uxOrigin", value=args.origin.upper())
        await page.evaluate(
            "() => document.getElementById('uxOrigin')"
            ".dispatchEvent(new Event('change', { bubbles: true }))"
        )
        await asyncio.sleep(1.2)
        await page.select_option("select#uxDest", value=args.destination.upper())
        await page.select_option("select#ddlDate_1", value=args.day)
        await page.select_option("select#ddlMY_1", value=args.year_month)
        await page.check("input#uxCHKGroup")
        await page.evaluate(
            """
            () => {
              const g = document.getElementById('uxCHKGroup');
              if (!g.checked) g.checked = true;
              if (typeof creategroup === 'function') creategroup(g);
            }
            """
        )
        await asyncio.sleep(0.8)
        await page.select_option("select#optAdult", value=str(args.adults))
        await page.select_option("select#optCurrency", value=args.currency)
        try:
            await page.evaluate("ValidateFlightAvailability();")
        except Exception:
            await page.click("a:has-text('Next')")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)
        print("  ✅ Search submitted")

        await browser.close()

    raw_path = out_dir / f"yeti_network_{stamp}.json"
    raw_path.write_text(json.dumps(events, indent=2), encoding="utf-8")

    # Summarize ASMX method hits
    methods: dict[str, int] = {}
    for ev in events:
        m = re.search(r"/WebService/([A-Za-z]+Service)\.asmx/([A-Za-z0-9_]+)", ev["url"])
        if m:
            key = f"{m.group(1)}.{m.group(2)}"
            methods[key] = methods.get(key, 0) + 1
        elif "/WebService/" in ev["url"] and ".asmx" in ev["url"]:
            methods[ev["path"]] = methods.get(ev["path"], 0) + 1

    summary = {
        "captured_at": stamp,
        "portal": PORTAL_URL,
        "agency": args.agency_name,
        "user": args.user_login,
        "sector": f"{args.origin}-{args.destination}",
        "date": f"{args.day}/{args.year_month}",
        "total_events": len(events),
        "asmx_methods_called": dict(sorted(methods.items(), key=lambda x: (-x[1], x[0]))),
        "raw_file": str(raw_path),
    }
    summary_path = out_dir / f"yeti_network_{stamp}.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== ASMX methods observed ===")
    if methods:
        for k, v in summary["asmx_methods_called"].items():
            print(f"  {v:3d}×  {k}")
    else:
        print("  (none matched — open the raw JSON; may be form posts to User.aspx)")
    print(f"\nRaw:     {raw_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
