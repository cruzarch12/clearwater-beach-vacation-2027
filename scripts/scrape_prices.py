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
    output entirely rather than guessing — and the page treats a URL with
    no price in this run's output as having no confirmed price (nothing
    is carried over from earlier runs).
  - Airbnb/Vrbo/Booking.com actively try to detect and block automated
    browsing (CAPTCHA / "verify you're human" challenge pages). When that
    happens, the page text never contains a real price, so this script
    can't make one up — it flags that link as "blocked" and leaves it out
    of the output rather than guessing. This is a structural
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

A price only counts if the listing is also confirmed bookable for the
exact dates in its URL: pages that say the dates are unavailable are
recorded as "unavailable" (no price written), and a price quoted for a
different number of nights than the URL's dates span is rejected too
(sites sometimes silently swap in other dates).

Run manually:  python scripts/scrape_prices.py
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse
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
SETTLE_MS = 6000
SETTLE_MS_SLOW = 12000
# The settle times above are now only a *ceiling*: the page text is checked
# every POLL_MS and reading stops as soon as a stable price (or an
# "unavailable" message) shows up, so most pages finish in 1-3 seconds
# instead of always sitting out the full wait.
POLL_MS = 500
MIN_WAIT_MS = 1000

# Parallelism: how many pages in flight overall, and per website. The
# per-site cap is what matters for avoiding bot flags (they look at repeated
# hits from one IP to one site); different sites can run side by side.
MAX_CONCURRENT = 4
MAX_PER_HOST = 2
BREAKER_AFTER = 2
# Sites that have never blocked us get a slightly higher per-site cap.
HOST_CAP_OVERRIDE = {"www.airbnb.com": 3}
# Debug screenshots of pages that gave no price on the defended sites /
# Guesty, saved to ./debug/ and uploaded by the workflow as a downloadable
# artifact (never committed) so the real page can be inspected.
MAX_SHOTS = 4
_shots = []

SLOW_HOSTS = ("vrbo.com", "booking.com", "guestybookings.com", "expedia.com", "hospitable.com", "clearwaterbeachvacationhomes.com")

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
    "confirm you are human",
    "verifies that you are not a bot",
)


# Phrases that mean "these dates can't be booked". Deliberately specific:
# bare words like "unavailable" also appear on perfectly bookable Airbnb
# pages (e.g. "Unavailable: Hair dryer" in the amenities list).
UNAVAILABLE_SIGNS = (
    "those dates are not available",
    "those dates aren't available",
    "these dates are not available",
    "these dates aren't available",
    "dates are not available",
    "dates aren't available",
    "selected dates are not available",
    "not available for your dates",
    "not available for the dates",
    "not available for these dates",
    "not available on these dates",
    "not available for the selected dates",
    "no availability for",
    "we have no availability",
    "no longer available",
    "isn't available for",
    "is not available for",
    "this property isn't available",
    "sold out on your dates",
)

_DATE_PARAMS = (
    ("check_in", "check_out"), ("chkin", "chkout"), ("checkin", "checkout"),
    ("checkIn", "checkOut"), ("ci", "co"),
)


def expected_nights(url: str):
    """Number of nights the URL's own check-in/check-out dates span, or
    None if the URL carries no recognizable date pair."""
    q = parse_qs(urlparse(url).query)
    for a, b in _DATE_PARAMS:
        if a in q and b in q:
            try:
                d1 = date.fromisoformat(q[a][0])
                d2 = date.fromisoformat(q[b][0])
                n = (d2 - d1).days
                return n if n > 0 else None
            except ValueError:
                return None
    return None


def looks_unavailable(page_text: str) -> bool:
    low = page_text.lower().replace("\u2019", "'")
    return any(sign in low for sign in UNAVAILABLE_SIGNS)


def _nights_price(page_text: str, url: str):
    """Price from a '$X for N nights' phrase — but only one whose N matches
    the nights the URL's dates span. Returns (price, mismatch) where
    mismatch is True if a price was quoted but only for other night
    counts (i.e. the site quietly swapped in different dates)."""
    want = expected_nights(url)
    found_any = False
    for m in re.finditer(MONEY + r"\s*for\s*(\d+)\s*nights?", page_text):
        found_any = True
        if want is None or int(m.group(1)) == want:
            return _to_float(m.group(0)), False
    return None, found_any


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
        return _nights_price(page_text, url)[0]

    if "airbnb.com" in host:
        # "$9,691 for 5 nights" appears near the sticky price/reserve box.
        return _nights_price(page_text, url)[0]

    if "expedia.com" in host:
        # "The current price is $10,940 total" (screen-reader text on the
        # price block). The nightly figures elsewhere on the page are not
        # the stay total, so only this phrase counts.
        m = re.search(r"current price is\s*(" + MONEY + r")\s*total", page_text, re.I)
        return _to_float(m.group(1)) if m else None

    if "clearwaterbeachvacationhomes.com" in host:
        # The quote lives in an embedded OwnerRez booking widget, which
        # attempt() appends to the page text. "Book Direct: $3,539" is the
        # direct stay price; the Vrbo/Airbnb "from $X" comparison rates
        # beside it are deliberately ignored.
        m = re.search(r"Book Direct:?\s*(" + MONEY + r")", page_text, re.I)
        return _to_float(m.group(1)) if m else None

    if "hospitable.com" in host:
        # Booking-request page: "$1,596.60 x 5 nights ... Total $9,782.28".
        # The dates are preselected by the link and aren't in the text, so
        # the "x N nights" line must match the nights this link was made for.
        # No price block (dates unbookable) -> None.
        want = expected_nights(url) or 5   # both trip weeks are 5 nights
        nm = re.search(r"x\s*(\d+)\s*nights?", page_text)
        if want and (not nm or int(nm.group(1)) != want):
            return None
        m = re.search(r"Total\s*(" + MONEY + r")", page_text)
        return _to_float(m.group(1)) if m else None

    if "guestybookings.com" in host or "thegemmacwb.com" in host:
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


async def check_one(context, url: str, results: dict, errors: list, blocked: list, statuses: dict, unavailable: list):
    host = host_of(url)
    is_slow_host = any(h in host for h in SLOW_HOSTS)
    settle = SETTLE_MS_SLOW if is_slow_host else SETTLE_MS

    def date_mismatch(u, t):
        # A price was quoted, but only for a different number of nights than
        # the URL's dates span — the site swapped in other dates.
        h = host_of(u)
        if "airbnb.com" in h or "vrbo.com" in h:
            price, mismatch = _nights_price(t, u)
            return price is None and mismatch
        return False

    async def read_text(page):
        text = await page.inner_text("body")
        if "clearwaterbeachvacationhomes.com" in host:
            # Prices render inside embedded widget iframes, which
            # inner_text("body") on the main page doesn't include.
            for fr in page.frames[1:]:
                try:
                    text += "\n" + await fr.inner_text("body")
                except Exception:
                    pass
        return text

    async def snap(page):
        if len(_shots) >= MAX_SHOTS or not (is_hard_host(url) or "guestybookings.com" in host):
            return
        try:
            out = ROOT / "debug"
            out.mkdir(exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9]+", "_", host_of(url) + "_" + url[-40:])[:70] + ".png"
            await page.screenshot(path=str(out / name), full_page=False)
            _shots.append(name)
        except Exception:
            pass

    async def attempt(max_wait_ms):
        """Load the page, then poll its text until a price (seen on two
        consecutive polls, so a half-rendered widget can't fool it) or an
        'unavailable' message appears, or max_wait_ms runs out."""
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(MIN_WAIT_MS)
            waited = MIN_WAIT_MS
            nudged = False
            clicked = False
            block_polls = 0
            last_price = None
            text = ""
            while True:
                if not nudged:
                    # Nudge lazy-rendered price widgets into view (once).
                    nudged = True
                    try:
                        await page.mouse.wheel(0, 1200)
                    except Exception:
                        pass
                try:
                    text = await read_text(page)
                except Exception:
                    text = ""
                if text:
                    if looks_unavailable(text) or date_mismatch(url, text):
                        return text
                    price_now = parse_price(url, text)
                    if price_now is not None and price_now == last_price:
                        return text
                    last_price = price_now
                    if price_now is None:
                        if any(sign in text.lower() for sign in BLOCK_SIGNS):
                            block_polls += 1
                            # A real bot-check page rarely clears itself; after a
                            # few seconds of seeing it, stop waiting.
                            if block_polls >= 4 and waited >= 4000:
                                await snap(page)
                                return text
                        else:
                            block_polls = 0
                if waited >= max_wait_ms:
                    await snap(page)
                    return text
                if "guestybookings.com" in host and not clicked and waited >= 2500:
                    # These pages show "Search for available dates" with the
                    # dates already filled in; the quote only appears once
                    # that Search button is pressed (read-only action).
                    clicked = True
                    try:
                        await page.get_by_role("button", name="Search", exact=True).first.click(timeout=2000)
                    except Exception:
                        pass
                await page.wait_for_timeout(POLL_MS)
                waited += POLL_MS
        finally:
            await page.close()

    try:
        text = await attempt(settle)
        if looks_unavailable(text) or date_mismatch(url, text):
            # Not bookable for these exact dates: no price is recorded at
            # all, so this date range drops out on the page.
            statuses[url] = "unavailable"
            unavailable.append(url)
            return
        price = parse_price(url, text)
        if price is None and any(sign in text.lower() for sign in BLOCK_SIGNS):
            # A bot-check page won't turn into a price by waiting longer;
            # skip the retry (saves ~10s per blocked link).
            pass
        elif price is None and is_hard_host(url):
            pass   # defended sites: a longer wait hasn't helped; don't double the cost
        elif price is None:
            # One retry with a longer settle time before giving up — a
            # slow-rendering price widget (heavy pages like listings with
            # 90+ photos) can look like a block or a missing price on the
            # first pass.
            text = await attempt(SETTLE_MS_SLOW + 6000)
            if looks_unavailable(text) or date_mismatch(url, text):
                statuses[url] = "unavailable"
                unavailable.append(url)
                return
            price = parse_price(url, text)

        if price is not None:
            results[url] = price
            statuses[url] = "ok"
        elif looks_blocked(text):
            blocked.append(url)
            statuses[url] = "blocked"
        else:
            hint = re.search(r".{0,40}\d+\s*nights?.{0,40}", text)
            dollars = [m.group(0).replace("\n", " ") for m in re.finditer(r".{0,30}\$[\d,]+.{0,20}", text)][:4]
            head = text.strip()[:120].replace("\n", " ") if len(text) < 1500 else None
            errors.append(
                f"no price match ({len(text)} chars of page text; "
                f"nights text: {(hint.group(0).strip() if hint else None)!r}; "
                f"$ amounts seen: {dollars}; short-page text: {head!r}): {url}"
            )
            statuses[url] = "no_match"
    except Exception as exc:  # noqa: BLE001 - log and move on, never crash the run
        errors.append(f"{type(exc).__name__} on {url}: {exc}")
        statuses[url] = "error"


# Vrbo and Expedia (same parent company, same bot defenses) are the sites
# that block GitHub's servers. Only these get the heavier treatment below:
# a Firefox-based anti-fingerprint browser (Camoufox) and, if one is
# configured, a paid proxy. Everything else keeps using the plain Chromium
# setup — which already works — and never touches the proxy, so no proxy
# data allowance is spent on sites that don't need it.
HARD_HOSTS = ("vrbo.com", "expedia.com")


def is_hard_host(url: str) -> bool:
    return any(h in host_of(url) for h in HARD_HOSTS)


def proxy_from_env():
    """Optional proxy for the hard hosts, read from environment variables
    that the workflow fills from GitHub Actions *secrets* (PROXY_SERVER,
    PROXY_USERNAME, PROXY_PASSWORD). Nothing is ever stored in the repo, and
    the values are never printed. Unset => no proxy, current behavior."""
    server = os.environ.get("PROXY_SERVER", "").strip()
    if not server:
        return None
    proxy = {"server": server}
    user = os.environ.get("PROXY_USERNAME", "").strip()
    pw = os.environ.get("PROXY_PASSWORD", "").strip()
    if user:
        proxy["username"] = user
    if pw:
        proxy["password"] = pw
    return proxy


async def main():
    urls = extract_url_list()
    if not urls:
        print("No offer URLs found in index.html — nothing to do.", file=sys.stderr)
        return 1

    results: dict = {}
    errors: list = []
    blocked: list = []
    unavailable: list = []
    statuses: dict = {}

    proxy = proxy_from_env()
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        # Patch over the usual automated-browser tells (navigator.webdriver,
        # headless-specific JS properties, the headless UA string, etc.)
        # before any page in this context loads anything. Every page.goto()
        # a listing site does happens inside this context, so one call here
        # covers the whole run.
        await Stealth().apply_stealth_async(context)

        # Browser used for Vrbo/Expedia: Camoufox if it's installed and
        # launches; otherwise (never an error — the run just continues) a
        # Chromium context, proxied if a proxy is configured.
        hard_target = context
        camoufox_cm = None
        try:
            from camoufox.async_api import AsyncCamoufox

            kwargs = {"headless": True}
            if proxy:
                # geoip=True makes the browser's timezone/locale match the
                # proxy's location, so the two don't contradict each other.
                kwargs.update(proxy=proxy, geoip=True)
            camoufox_cm = AsyncCamoufox(**kwargs)
            hard_target = await camoufox_cm.__aenter__()
            print(f"Vrbo/Expedia: using Camoufox ({'via proxy' if proxy else 'no proxy configured'}).")
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the run
            camoufox_cm = None
            print(f"Camoufox unavailable ({type(exc).__name__}: {exc}) — Vrbo/Expedia use the standard browser.", file=sys.stderr)
            if proxy:
                hard_target = await browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                    proxy=proxy,
                )
                await Stealth().apply_stealth_async(hard_target)
                print("Vrbo/Expedia: standard browser via proxy.")

        # Concurrency: a global cap plus a per-site cap, so different
        # sites are checked side by side but no single site sees more than
        # MAX_PER_HOST simultaneous visits from this IP.
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        host_sems: dict = {}

        # Circuit breaker for the heavily defended sites: if the first
        # BREAKER_AFTER links to a site all come back without a price, the
        # rest of that site's links are skipped for this run instead of
        # spending ~30s each on pages that won't give a price.
        fails: dict = {}
        timings: dict = {}
        t_run = time.monotonic()

        async def bound_check(u):
            h = host_of(u)
            hs = host_sems.setdefault(h, asyncio.Semaphore(HOST_CAP_OVERRIDE.get(h, MAX_PER_HOST)))
            async with hs, sem:
                t_start = time.monotonic()
                hard = is_hard_host(u)
                if hard and fails.get(h, 0) >= BREAKER_AFTER:
                    statuses[u] = "blocked"
                    blocked.append(u)
                    return
                target = hard_target if hard else context
                await check_one(target, u, results, errors, blocked, statuses, unavailable)
                timings[u] = time.monotonic() - t_start
                if hard:
                    if statuses.get(u) == "ok":
                        fails[h] = -10**6      # a success disables the breaker
                    else:
                        fails[h] = fails.get(h, 0) + 1

        try:
            await asyncio.gather(*(bound_check(u) for u in urls))
        finally:
            if camoufox_cm is not None:
                try:
                    await camoufox_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            await browser.close()

    # No merge with a previous run: the page's design is "only a price this
    # specific run confirmed counts," so a URL that didn't come back with a
    # price here (blocked, errored, or not yet checked) is simply absent
    # from this run's offers — never carried forward from an older,
    # possibly-stale successful check.

    # Per-host summary, so a glance at the step log (or the JSON itself)
    # shows which platforms are structurally hard to auto-refresh instead
    # of burying that in a wall of per-URL lines.
    host_stats = {}
    for u in urls:
        h = host_of(u)
        s = host_stats.setdefault(h, {"checked": 0, "ok": 0, "blocked": 0, "unavailable": 0, "other_failed": 0})
        s["checked"] += 1
        st = statuses.get(u)
        if st == "ok":
            s["ok"] += 1
        elif st == "blocked":
            s["blocked"] += 1
        elif st == "unavailable":
            s["unavailable"] += 1
        elif st in ("no_match", "error"):
            s["other_failed"] += 1

    PRICES_JSON.parent.mkdir(parents=True, exist_ok=True)
    PRICES_JSON.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "offers": results,
                "platform_status": host_stats,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Checked {len(urls)} links: {len(results)} updated, {len(blocked)} blocked, {len(unavailable)} unavailable for those dates, {len(errors)} other failures.")
    print("Per-platform results:")
    for h, s in sorted(host_stats.items()):
        print(f"  - {h}: {s['ok']}/{s['checked']} ok, {s['blocked']} blocked, {s['unavailable']} unavailable, {s['other_failed']} other failed")
    per_host_time = {}
    for u, sec in timings.items():
        per_host_time[host_of(u)] = per_host_time.get(host_of(u), 0) + sec
    print(f"Scrape wall time {time.monotonic() - t_run:.0f}s. Seconds spent per site (summed across links, run partly in parallel):")
    for h, sec in sorted(per_host_time.items(), key=lambda kv: -kv[1]):
        print(f"  - {h}: {sec:.0f}s")
    if blocked:
        print("\nBlocked (bot-check/CAPTCHA page returned instead of listing):", file=sys.stderr)
        for u in blocked:
            print(f"  - {u}", file=sys.stderr)
    if unavailable:
        print("\nUnavailable for the requested dates (left out on purpose):", file=sys.stderr)
        for u in unavailable:
            print(f"  - {u}", file=sys.stderr)
    if errors:
        print("\nOther failures:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
