#!/usr/bin/env python3
"""
Re-checks every booking link used by the Clearwater Beach rental map and
writes the results to data/prices.json as {url: price} pairs, keyed by the
*exact* URL used in index.html's `properties` array (so the page can match
them up with a plain lookup — no fuzzy matching).

This is a best-effort scraper, not an official API integration (none of
these platforms offer one to individual travelers). Each site renders its
price with JavaScript, so this uses Playwright (a real headless browser)
rather than a plain HTTP request, and pulls the price out of the page's
visible text with a small, source-specific regex. That means:

  - A listing's HTML can change at any time and quietly break its pattern.
    When a pattern doesn't match, this script leaves that URL out of the
    output entirely rather than guessing — the page then just falls back
    to whatever price it already has (either from a previous run, or the
    snapshot baked into index.html).
  - Airbnb/Vrbo/Booking.com actively try to detect and block automated
    browsing. Some runs may come back with fewer prices than others simply
    because a request got rate-limited or shown a verification challenge.
    That's expected — this is why the page shows a "last updated"
    timestamp rather than claiming every number is always fresh.

Run manually:  python scripts/scrape_prices.py
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROPERTIES_HTML = ROOT / "index.html"
PRICES_JSON = ROOT / "data" / "prices.json"

# How long to let a page sit after load before reading its text — these
# sites render the price box client-side, so grabbing text too early comes
# back empty.
SETTLE_MS = 3500

# One combined dollar-amount pattern, used differently per platform below.
MONEY = r"\$[\d,]+(?:\.\d{2})?"


def extract_url_list():
    """Pull every offer URL straight out of index.html's properties array,
    so this script and the page can never drift out of sync with each
    other about which links exist."""
    text = PROPERTIES_HTML.read_text(encoding="utf-8")
    urls = re.findall(r'url:"(https?://[^"]+)"', text)
    # de-dupe while preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_price(url: str, page_text: str):
    """Site-specific price extraction. Returns a float or None."""
    host = re.search(r"https?://([^/]+)/", url + "/")
    host = host.group(1) if host else ""

    if "sunnyorangestays.com" in host:
        # "$7,009.20 total"
        m = re.search(MONEY + r"\s*total", page_text)
        return _to_float(m.group(0)) if m else None

    if "vrbo.com" in host:
        # "$11,028 for 5 nights" appears once, near the booking widget.
        m = re.search(MONEY + r"\s*for\s*\d+\s*nights?", page_text)
        return _to_float(m.group(0)) if m else None

    if "airbnb.com" in host:
        # "$9,691 for 5 nights" appears near the sticky price/reserve box.
        m = re.search(MONEY + r"\s*for\s*\d+\s*nights?", page_text)
        return _to_float(m.group(0)) if m else None

    if "guestybookings.com" in host:
        # Booking-summary panel ends with "Total  $11,157.62"
        m = re.search(r"Total\s*" + MONEY, page_text)
        return _to_float(m.group(0)) if m else None

    if "booking.com" in host:
        # "Price $11,576"
        m = re.search(r"Price\s*" + MONEY, page_text)
        return _to_float(m.group(0)) if m else None

    if "whimstay.com" in host:
        # Whimstay's search price is usually passed in the URL itself
        # (searchPrice=...); try the live page text first, fall back to
        # that query parameter.
        m = re.search(MONEY + r"\s*(?:total|for\s*\d+\s*nights?)", page_text)
        if m:
            return _to_float(m.group(0))
        m = re.search(r"[?&]searchPrice=(\d+(?:\.\d+)?)", url)
        return float(m.group(1)) if m else None

    return None


def _to_float(money_str: str):
    digits = re.search(r"[\d,]+(?:\.\d{2})?", money_str)
    if not digits:
        return None
    return float(digits.group(0).replace(",", ""))


async def check_one(context, url: str, results: dict, errors: list):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(SETTLE_MS)
        text = await page.inner_text("body")
        price = parse_price(url, text)
        if price is not None:
            results[url] = price
        else:
            errors.append(f"no price match: {url}")
    except Exception as exc:  # noqa: BLE001 - log and move on, never crash the run
        errors.append(f"{type(exc).__name__} on {url}: {exc}")
    finally:
        await page.close()


async def main():
    urls = extract_url_list()
    if not urls:
        print("No offer URLs found in index.html — nothing to do.", file=sys.stderr)
        return 1

    existing = {}
    if PRICES_JSON.exists():
        existing = json.loads(PRICES_JSON.read_text(encoding="utf-8")).get("offers", {})

    results: dict = {}
    errors: list = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        # Small concurrency cap — hammering these sites in parallel is what
        # gets an IP flagged fastest.
        sem = asyncio.Semaphore(3)

        async def bound_check(u):
            async with sem:
                await check_one(context, u, results, errors)

        await asyncio.gather(*(bound_check(u) for u in urls))
        await browser.close()

    # Merge: keep the previous value for anything this run couldn't read.
    merged = dict(existing)
    merged.update(results)

    PRICES_JSON.parent.mkdir(parents=True, exist_ok=True)
    PRICES_JSON.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "offers": merged,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Checked {len(urls)} links: {len(results)} updated, {len(errors)} failed.")
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
