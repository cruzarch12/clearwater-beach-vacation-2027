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
    browsing (CAPTCHA / "verify you're human" challenge pages). When that
    happens, the page text never contains a real price, so this script
    can't make one up — it flags that link as "blocked" and keeps the
    last known good price rather than guessing. This is a structural
    limit of these sites for *any* unofficial automated check, not a bug
    specific to this script. The output records which URLs were blocked
    each run so the page can be honest about it instead of silently
    pretending everything refreshed.

    To cut down on how often that happens, every browser context gets
    playwright-stealth's evasions applied (hides the usual automation
    fingerprints — navigator.webdriver, headless-only JS properties, the
    headless user-agent string, etc.) before any page loads. It's not a
    guarantee against a site as defended as Vrbo's, but it's a real,
    free improvement over an untouched headless Chromium, which is what
    most of these sites flag almost immediately.

Run manually:  python scripts/scrape_prices.py
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

ROOT = Path(__file__).resolve().parent.parent
PROPERTIES_HTML = ROOT / "index.html"
PRICES_JSON = ROOT / "data" / "prices.json"

# How long to let a page sit after load before reading its text — these
# sites render the price box client-side, so grabbing text too early comes
# back empty. Platforms known to be slower / more defensive get a longer
# settle time and one retry below.
SETTLE_MS = 3500
SETTLE_MS_SLOW = 7000

SLOW_HOSTS = ("vrbo.com", "booking.com", "guestybookings.com")

# One combined dollar-amount pattern, used differently per platform below.
MONEY = r"\$[\d,]+(?:\.\d{2})?"

# Phrases that show up on bot-detection / CAPTCHA / "verify you're human"
# interstitials instead of real listing content. If the rendered page text
# contains one of these (and no price matched), we call it "blocked"
# rather than "no price match" — the two mean very different things to
# someone reading the log.
BLOCK_SIGNS = (
    "captcha",
    "verify you are human",
    "verify you're human",
    "are you a human",
    "pardon the interruption",
    "unusual traffic",
    "access denied",
    "access to this page has been denied",
    "request blocked",
    "automated access",
    "robot check",
    "please enable javascript and cookies",
    "checking your browser",
    "ddos protection by",
    "attention required",
)


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


def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)/", url + "/")
    return m.group(1) if m else ""


def looks_blocked(page_text: str) -> bool:
    low = page_text.lower()
    if any(sign in low for sign in BLOCK_SIGNS):
        return True
    # A real listing page has thousands of characters of visible text
    # (amenities, reviews, nav, etc). A bot-check interstitial is usually
    # just a few short lines. This catches blocks that don't match any
    # known phrase above.
    if len(page_text.strip()) < 300:
        return True
    return False


def parse_price(url: str, page_text: str):
    """Site-specific price extraction. Returns a float or None."""
    host = host_of(url)

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


async def check_one(context, url: str, results: dict, errors: list, blocked: list, statuses: dict):
    host = host_of(url)
    is_slow_host = any(h in host for h in SLOW_HOSTS)
    settle = SETTLE_MS_SLOW if is_slow_host else SETTLE_MS

    async def attempt(settle_ms):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(settle_ms)
            # Nudge lazy-rendered price widgets into view.
            try:
                await page.mouse.wheel(0, 1200)
                await page.wait_for_timeout(600)
            except Exception:
                pass
            return await page.inner_text("body")
        finally:
            await page.close()

    try:
        text = await attempt(settle)
        price = parse_price(url, text)
        if price is None and looks_blocked(text):
            # One retry with a longer settle time before giving up — a
            # slow-rendering widget can look like a block on the first
            # pass.
            text = await attempt(SETTLE_MS_SLOW + 3000)
            price = parse_price(url, text)

        if price is not None:
            results[url] = price
            statuses[url] = "ok"
        elif looks_blocked(text):
            blocked.append(url)
            statuses[url] = "blocked"
        else:
            errors.append(f"no price match: {url}")
            statuses[url] = "no_match"
    except Exception as exc:  # noqa: BLE001 - log and move on, never crash the run
        errors.append(f"{type(exc).__name__} on {url}: {exc}")
        statuses[url] = "error"


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
    blocked: list = []
    statuses: dict = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        # Patch over the usual automated-browser tells (navigator.webdriver,
        # headless-specific JS properties, the headless UA string, etc.)
        # before any page in this context loads anything. Every page.goto()
        # a listing site does happens inside this context, so one call here
        # covers the whole run.
        await Stealth().apply_stealth_async(context)
        # Small concurrency cap — hammering these sites in parallel is what
        # gets an IP flagged fastest.
        sem = asyncio.Semaphore(3)

        async def bound_check(u):
            async with sem:
                await check_one(context, u, results, errors, blocked, statuses)

        await asyncio.gather(*(bound_check(u) for u in urls))
        await browser.close()

    # Merge: keep the previous value for anything this run couldn't read.
    merged = dict(existing)
    merged.update(results)

    # Per-host summary, so a glance at the step log (or the JSON itself)
    # shows which platforms are structurally hard to auto-refresh instead
    # of burying that in a wall of per-URL lines.
    host_stats = {}
    for u in urls:
        h = host_of(u)
        s = host_stats.setdefault(h, {"checked": 0, "ok": 0, "blocked": 0, "other_failed": 0})
        s["checked"] += 1
        st = statuses.get(u)
        if st == "ok":
            s["ok"] += 1
        elif st == "blocked":
            s["blocked"] += 1
        elif st in ("no_match", "error"):
            s["other_failed"] += 1

    PRICES_JSON.parent.mkdir(parents=True, exist_ok=True)
    PRICES_JSON.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "offers": merged,
                "platform_status": host_stats,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Checked {len(urls)} links: {len(results)} updated, {len(blocked)} blocked, {len(errors)} other failures.")
    print("Per-platform results:")
    for h, s in sorted(host_stats.items()):
        print(f"  - {h}: {s['ok']}/{s['checked']} ok, {s['blocked']} blocked, {s['other_failed']} other failed")
    if blocked:
        print("\nBlocked (bot-check/CAPTCHA page returned instead of listing):", file=sys.stderr)
        for u in blocked:
            print(f"  - {u}", file=sys.stderr)
    if errors:
        print("\nOther failures:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
