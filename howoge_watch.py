#!/usr/bin/env python3
"""
howoge_watch.py
Watches HOWOGE (and later other Berlin landeseigene) for new flats and pushes a
Telegram alert the second a match appears.

Designed to run 24/7 on any always-on machine (old laptop, Raspberry Pi, small VPS).

Filter logic: price (warm rent) <= MAX_WARM_RENT  AND  postal code in ALLOWED_PLZ.
Rooms are NOT filtered (price is the gate).

Architecture: each landlord is a "source" = a function returning a list[Listing].
Source functions receive (seen: set, debug: bool). Add a new source -> append it to
SOURCES. Everything else (filter, dedup, alert) is shared.

Why a headless browser: the HOWOGE search renders listings with JavaScript, so a
plain requests.get() returns an empty page. Playwright loads the page like a real
browser and reads the rendered cards.

----------------------------------------------------------------------
SETUP (once)
----------------------------------------------------------------------
1. Install Python 3.10+
2. pip install playwright requests
3. playwright install chromium
4. Make a Telegram bot:
   - open Telegram, search @BotFather, send /newbot, follow prompts
   - copy the token it gives you
5. Get your chat id:
   - message your new bot once (say "hi")
   - open: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   - find "chat":{"id": ...} -> that number is your chat id
6. Set env vars (or hardcode below, less safe):
   export HOWOGE_TG_TOKEN="123456:ABC..."
   export HOWOGE_TG_CHAT="987654321"
7. Run: python3 howoge_watch.py
   First run sends a status ping so you know Telegram works.

Debug a source whose selectors stopped matching:
   python3 howoge_watch.py --debug
   -> dumps the rendered HTML to debug_howoge.html so the card structure can be
      re-checked.
----------------------------------------------------------------------
"""

import os
import re
import sys
import json
import time
import html
import queue
import random
import logging
import multiprocessing as mp
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

TG_TOKEN = os.environ.get("HOWOGE_TG_TOKEN", "")   # from @BotFather
TG_CHAT  = os.environ.get("HOWOGE_TG_CHAT", "")    # your numeric chat id

MAX_WARM_RENT         = int(os.environ.get("MAX_WARM_RENT", "1400"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "240"))
MAX_DETAIL_RETRIES    = int(os.environ.get("MAX_DETAIL_RETRIES", "3"))

# Hard wall-clock budget for one scrape cycle. The browser scrape runs in a child
# process; if it exceeds this (e.g. a Playwright call hangs forever), the parent
# kills the child and continues. This is what prevents the whole watcher from
# wedging on a hung native call — try/except cannot catch a hang.
CYCLE_TIMEOUT_SECONDS = int(os.environ.get("CYCLE_TIMEOUT_SECONDS", "180"))

# Random extra delay (0..N seconds) added to each poll so requests aren't a
# perfectly regular beat — reduces the bot-like fingerprint that invites throttling.
POLL_JITTER_SECONDS = int(os.environ.get("POLL_JITTER_SECONDS", "60"))

# Timeout for launching Chromium itself (Playwright default is 30s, but we set it
# explicitly so a stuck launch raises instead of blocking indefinitely).
BROWSER_LAUNCH_TIMEOUT_MS = int(os.environ.get("BROWSER_LAUNCH_TIMEOUT_MS", "60000"))

# When a block/throttle is detected, send at most one Telegram warning per this
# many seconds (default 1h) so we don't spam a message every poll while blocked.
BLOCK_NOTIFY_INTERVAL_SECONDS = int(os.environ.get("BLOCK_NOTIFY_INTERVAL_SECONDS", "3600"))

# A "failed cycle" is one where the scrape subprocess errored/timed out or every
# source raised — i.e. we got NO usable data, as opposed to a clean run that
# genuinely found nothing new. These are the failures that silently freeze the
# "seen" counter (e.g. Chromium crashing on launch), so we surface and self-heal:
#   * FAILURE_NOTIFY_INTERVAL_SECONDS: min seconds between failure alerts (rate limit)
#   * MAX_CONSECUTIVE_FAILURES: after this many failed cycles in a row, exit non-zero
#     so the platform (Railway restartPolicy=ALWAYS) recycles us into a FRESH
#     container. The watcher's own catch-and-continue loop otherwise keeps the
#     process alive forever, so the container never restarts and never escapes a
#     wedged environment. 0 disables the self-restart.
FAILURE_NOTIFY_INTERVAL_SECONDS = int(os.environ.get("FAILURE_NOTIFY_INTERVAL_SECONDS", "3600"))
MAX_CONSECUTIVE_FAILURES        = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5"))

# After MAX_DETAIL_RETRIES failed enrichments, decide what to do with a listing
# whose postal code could still not be determined:
#   True  -> send a flagged alert so nothing is silently missed
#   False -> mark seen and skip silently
ALERT_ON_UNVERIFIED = True

# Relevant postal codes. Prenzlauer Berg, Pankow (Ortsteil), Kreuzberg,
# Friedrichshain, North Neukoelln. Edit freely.
ALLOWED_PLZ = {
    # Prenzlauer Berg
    "10119", "10405", "10407", "10409", "10435", "10437", "10439",
    # Pankow (Ortsteil)
    "13187", "13189",
    # Kreuzberg
    "10961", "10963", "10965", "10967", "10969", "10997", "10999",
    # Friedrichshain
    "10243", "10245", "10247", "10249",
    # North Neukoelln
    "12043", "12045", "12047", "12049", "12053",
}

# On Railway: set SEEN_PATH=/data/seen_listings.json and mount a volume at /data
SEEN_FILE = Path(os.environ.get("SEEN_PATH", "seen_listings.json"))
# Tracks the last time we sent a block/throttle alert (file-based so it survives
# across the per-cycle scrape subprocesses, which don't share memory).
BLOCK_NOTIFY_FILE = SEEN_FILE.parent / ".block_notified"
# Same idea for scrape-failure alerts (browser crash, timeout, all sources down).
FAILURE_NOTIFY_FILE = SEEN_FILE.parent / ".failure_notified"
HEADLESS   = True
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watch")


# ----------------------------------------------------------------------
# DATA MODEL
# ----------------------------------------------------------------------

@dataclass
class Listing:
    uid: str          # stable unique id, e.g. "howoge:1770-20272-258"
    source: str
    title: str
    address: str
    plz: str
    rooms: str
    size: str
    warm_rent: float  # EUR, 0.0 if unknown
    wbs: str          # "ja" / "nein" / "?"
    url: str


# ----------------------------------------------------------------------
# SEEN-STATE (dedup so each flat alerts only once)
# ----------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        log.error("Telegram token/chat not set. Set HOWOGE_TG_TOKEN and HOWOGE_TG_CHAT.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }, timeout=20)
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def send_status_ping(seen: set, daily_new: int, daily_matches: int, label: str) -> None:
    date_str = datetime.now().strftime("%y%m%d")
    poll_m = max(1, POLL_INTERVAL_SECONDS // 60)
    msg = (f"\U0001F916 {date_str}: {len(seen)} seen, {daily_new} new, "
           f"{daily_matches} matched, poll {poll_m}m")
    tg_send(msg)


def format_alert(li: Listing, unverified: bool = False) -> str:
    rent = f"{li.warm_rent:.0f} EUR warm" if li.warm_rent else "rent ?"
    head = ""
    if unverified:
        head = ("⚠️ UNVERIFIED: detail page could not be read, "
                "postal code/rent not confirmed. Check the link.\n")
    return (
        head
        + f"\U0001F3E0 <b>{html.escape(li.title or 'Wohnung')}</b>\n"
        + f"{html.escape(li.address)}\n"
        + f"{rent} | {html.escape(li.rooms)} Zi | {html.escape(li.size)}\n"
        + f"WBS: {html.escape(li.wbs)} | {li.source}\n"
        + f"{li.url}"
    )


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

PLZ_RE = re.compile(r"\b(1[0-3]\d{3})\b")          # Berlin PLZ 10xxx..13xxx
EURO_RE = re.compile(r"(\d[\d.\s]*[,]\d{2}|\d[\d.]*)\s*(?:EUR|€)")

# Safe rent labels: all mean total monthly warm rent.
# Excluded: bruttokaltmiete (no heating), gesamtkosten/gesamtbetrag (may
# include one-time fees), kaltmiete/nettokalt (cold rent only).
WARM_RE = re.compile(
    r"(bruttowarmmiete|warmmiete|warm|gesamtmiete)[^0-9]{0,20}([\d.]*,\d{2})",
    re.IGNORECASE
)

# Substrings whose numeric neighbors are NOT the monthly warm rent.
# Strip these (with a trailing window) before any fallback euro-parse.
_UNSAFE_RE = re.compile(r"kaution.{0,30}|kalt\w{0,10}.{0,20}", re.IGNORECASE)


def strip_rent_noise(text: str) -> str:
    return _UNSAFE_RE.sub("", text)


def parse_euro(text: str) -> float:
    """Parse German-formatted money like '1.234,56 EUR' -> 1234.56."""
    m = EURO_RE.search(text or "")
    if not m:
        return 0.0
    raw = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def extract_warm_rent(text_flat: str) -> float:
    """Extract warm rent from a block of text, stripping deposit/cold-rent noise first."""
    text_clean = strip_rent_noise(text_flat)
    mwarm = WARM_RE.search(text_clean)
    if mwarm:
        warm = parse_euro(mwarm.group(2))
        if warm:
            return warm
    return parse_euro(text_clean)


def first_plz(text: str) -> str:
    m = PLZ_RE.search(text or "")
    return m.group(1) if m else ""


def passes_filter(li: Listing) -> bool:
    if li.plz and li.plz not in ALLOWED_PLZ:
        return False
    if li.warm_rent and li.warm_rent > MAX_WARM_RENT:
        return False
    return True


# ----------------------------------------------------------------------
# SOURCE: HOWOGE
# ----------------------------------------------------------------------

HOWOGE_SEARCH = "https://www.howoge.de/immobiliensuche/wohnungssuche.html"
HOWOGE_BASE   = "https://www.howoge.de"

# Signals that a "no listings rendered" response is actually a block/throttle page
# rather than a genuinely empty result. Used to diagnose anti-bot measures.
BLOCK_SIGNALS = (
    "captcha", "cloudflare", "access denied", "zugriff verweigert",
    "rate limit", "too many requests", "request blocked", "are you human",
    "bot detection", "unusual traffic", "verify you are",
)


def _maybe_notify_block(status, hits) -> None:
    """Send a Telegram block/throttle warning, but at most once per
    BLOCK_NOTIFY_INTERVAL_SECONDS so we don't spam while blocked. State is kept in
    a file because each scrape cycle runs in a fresh subprocess (no shared memory)."""
    now = time.time()
    try:
        last = float(BLOCK_NOTIFY_FILE.read_text().strip()) if BLOCK_NOTIFY_FILE.exists() else 0.0
    except Exception:
        last = 0.0
    if now - last < BLOCK_NOTIFY_INTERVAL_SECONDS:
        return
    try:
        BLOCK_NOTIFY_FILE.write_text(str(now))
    except Exception:
        pass
    tg_send(f"⚠️ HOWOGE watcher: target appears to be blocking/throttling us "
            f"(http={status}, signals={', '.join(hits) if hits else 'none'}). "
            f"Still running; will keep retrying.")


def _maybe_notify_failure(consecutive: int, detail: str) -> None:
    """Alert that scrape cycles are failing outright (browser crash, timeout, or
    every source down) — the silent failure mode that freezes the 'seen' counter
    without ever tripping the block detector. Rate-limited via a file timestamp so
    the limit also holds across a self-restart and we don't spam while wedged."""
    now = time.time()
    try:
        last = float(FAILURE_NOTIFY_FILE.read_text().strip()) if FAILURE_NOTIFY_FILE.exists() else 0.0
    except Exception:
        last = 0.0
    if now - last < FAILURE_NOTIFY_INTERVAL_SECONDS:
        return
    try:
        FAILURE_NOTIFY_FILE.write_text(str(now))
    except Exception:
        pass
    tg_send(f"⛔ HOWOGE watcher: scrape is FAILING — no listings fetched for "
            f"{consecutive} cycle(s) in a row. Last error: {detail}. "
            f"A frozen 'seen' count right now means the scraper is down, not a quiet market.")


def diagnose_block(page, response) -> None:
    """On a render failure, log HTTP status + any block-page signals so we can tell
    a throttle/block apart from a genuinely empty page, and send a (rate-limited)
    Telegram alert when a block is detected. Best-effort; never raises."""
    status = None
    try:
        status = response.status if response else None
    except Exception:
        pass
    body = ""
    try:
        body = (page.content() or "").lower()
    except Exception:
        pass
    hits = [s for s in BLOCK_SIGNALS if s in body]
    if status in (403, 429) or hits:
        log.warning("HOWOGE: likely BLOCKED/THROTTLED (http=%s signals=%s)",
                    status, hits or "none")
        _maybe_notify_block(status, hits)
    else:
        log.warning("HOWOGE: no cards and no block signals (http=%s, len=%d) — "
                    "possibly transient or markup change", status, len(body))


def enrich_from_detail(browser, li: Listing) -> bool:
    """Open the listing's detail page and fill in missing plz/warm_rent on li in-place.

    Uses the already-running browser instance to avoid a second playwright launch.
    Returns True if the page loaded (even if data remains absent), False on error.

    NOTE: HOWOGE detail pages are Typo3 CMS — rent data is in server-rendered HTML,
    JS drives only gallery/map widgets. wait_until='load' is sufficient; the
    wait_for_selector below is a belt-and-suspenders guard.
    """
    detail_page = None
    try:
        detail_page = browser.new_page(user_agent=USER_AGENT)
        detail_page.goto(li.url, wait_until="load", timeout=30000)
        try:
            detail_page.wait_for_selector(
                "[class*='miete'],[class*='rent'],table", timeout=8000
            )
        except Exception:
            pass  # still attempt to parse; rent may be in static HTML

        text = detail_page.inner_text("body") or ""
        text_flat = " ".join(text.split())

        if not li.plz:
            li.plz = first_plz(text_flat)
        if not li.warm_rent:
            li.warm_rent = extract_warm_rent(text_flat)

        return True
    except Exception as e:
        log.warning("detail fetch failed for %s: %s", li.uid, e)
        return False
    finally:
        if detail_page:
            try:
                detail_page.close()
            except Exception:
                pass


def fetch_howoge(seen: set, debug: bool = False) -> list:
    """
    Load the HOWOGE search page in a headless browser, read the rendered listing
    cards, then enrich any new listings that are missing PLZ or warm rent by
    fetching their detail page within the same browser session.

    NOTE ON SELECTORS: HOWOGE can change its markup. The card selector below is a
    best effort. If a run finds 0 listings while the site clearly shows some, run
    with --debug, open debug_howoge.html, find the repeating card element, and
    update CARD_SEL / the field extraction lines marked TUNE.
    """
    from playwright.sync_api import sync_playwright

    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, timeout=BROWSER_LAUNCH_TIMEOUT_MS)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            response = page.goto(HOWOGE_SEARCH, wait_until="domcontentloaded", timeout=30000)

            # Wait for at least one listing card to appear before reading the DOM.
            # Failure here means the JS list didn't render; diagnose why (block vs
            # transient vs markup change), return [] and self-heal next cycle.
            try:
                page.wait_for_selector("a[href*='/wohnungssuche/detail/']", timeout=15000)
            except Exception:
                diagnose_block(page, response)
                return []

            listings = _parse_and_enrich(page, browser, seen, debug)
        finally:
            # Always tear the browser down, even on exception, so a Chromium
            # process can never be orphaned and leak memory across cycles.
            try:
                browser.close()
            except Exception:
                pass

    log.info("HOWOGE: parsed %d listings", len(listings))
    return listings


def _parse_and_enrich(page, browser, seen: set, debug: bool) -> list:
    """Read listing cards from a loaded search page and enrich new ones. Split out
    of fetch_howoge so the browser teardown can live in a single finally block."""
    listings = []

    if debug:
        Path("debug_howoge.html").write_text(page.content(), encoding="utf-8")
        log.info("Wrote debug_howoge.html")

    # TUNE: candidate selectors for a listing card. The script tries each in
    # order and uses the first that returns nodes.
    CARD_SELECTORS = [
        "a[href*='/wohnungssuche/detail/']",   # links straight to an expose
        "[class*='immo'] a[href*='detail']",
        "article a[href*='detail']",
    ]

    cards = []
    for sel in CARD_SELECTORS:
        cards = page.query_selector_all(sel)
        if cards:
            log.info("HOWOGE: matched %d nodes with '%s'", len(cards), sel)
            break

    seen_hrefs = set()
    for c in cards:
        href = c.get_attribute("href") or ""
        if "detail" not in href:
            continue
        if not href.startswith("http"):
            href = HOWOGE_BASE + href
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Pull text from the card and, ideally, its surrounding container.
        block = c
        try:
            parent = c.evaluate_handle("el => el.closest('article, li, div')")
            if parent:
                el = parent.as_element()
                if el:
                    block = el
        except Exception:
            pass
        text = (block.inner_text() if block else c.inner_text()) or ""
        text_flat = " ".join(text.split())

        # Stable id from the detail slug, e.g. .../detail/1770-20272-258.html
        mid = re.search(r"detail/([\w-]+)\.html", href)
        slug = mid.group(1) if mid else href
        uid = f"howoge:{slug}"

        plz  = first_plz(text_flat)
        warm = extract_warm_rent(text_flat)

        rooms = ""
        mr = re.search(r"(\d(?:[.,]\d)?)\s*Zimmer", text_flat, re.IGNORECASE)
        if mr:
            rooms = mr.group(1)

        size = ""
        ms = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*m", text_flat)
        if ms:
            size = ms.group(1) + " m2"

        wbs = "?"
        if re.search(r"\bWBS\b", text_flat, re.IGNORECASE):
            wbs = "ja"

        title = text_flat[:60] if text_flat else "HOWOGE Wohnung"

        listings.append(Listing(
            uid=uid, source="HOWOGE", title=title,
            address=(plz or "") + " " + text_flat[:80],
            plz=plz, rooms=rooms, size=size, warm_rent=warm, wbs=wbs, url=href,
        ))

    # Enrich new listings that are still missing PLZ or warm rent.
    # Only fetch detail pages for UIDs not already in seen — steady-state cost is zero.
    for li in listings:
        if li.uid in seen:
            continue
        if not li.plz or not li.warm_rent:
            log.info("enriching %s from detail page", li.uid)
            enrich_from_detail(browser, li)

    return listings


# Register sources here. To add degewo/gewobag/etc later, write a fetch_xxx()
# with signature (seen: set, debug: bool = False) -> list[Listing] and append it.
SOURCES = [fetch_howoge]


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

_RETRY: dict = {}  # uid -> number of inconclusive PLZ-unknown attempts


def _scrape_worker(q, seen_snapshot, debug):
    """Run all sources and put (status, payload, n_ok, detail) on the queue. Runs
    in a CHILD process so that a hung Playwright/Chromium call cannot wedge the
    watcher — the parent kills this process if it overruns CYCLE_TIMEOUT_SECONDS,
    and the child's exit reclaims all browser memory regardless of leaks.

    n_ok = how many sources returned without raising (an empty list still counts
    as a healthy run). detail = last source error, so the parent can tell a real
    outage (every source raised) apart from a clean-but-empty poll."""
    try:
        out = []
        n_ok = 0
        detail = ""
        for src in SOURCES:
            try:
                out.extend(src(seen_snapshot, debug=debug))
                n_ok += 1
            except Exception as e:
                detail = f"{getattr(src, '__name__', src)}: {e}"
                log.error("Source %s failed: %s", getattr(src, "__name__", src), e)
        q.put(("ok", out, n_ok, detail))
    except Exception as e:
        q.put(("err", [], 0, repr(e)))


def scrape_all(seen: set, debug: bool = False) -> tuple:
    """Run the scrape in a child process with a hard wall-clock timeout.

    Returns (listings, ok, detail):
      * ok=True  -> the cycle ran and at least one source returned data (possibly
                    an empty but legitimate result).
      * ok=False -> a real failure: the worker errored, was killed for overrunning,
                    or every source raised. `detail` explains it. This is the case
                    that must NOT be mistaken for a genuinely quiet market."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_scrape_worker, args=(q, set(seen), debug), daemon=True)
    p.start()
    n_ok, detail = 0, ""
    try:
        # Read the result first (not join-then-get) to avoid a queue/pipe deadlock.
        status, payload, n_ok, detail = q.get(timeout=CYCLE_TIMEOUT_SECONDS)
    except queue.Empty:
        log.error("scrape cycle exceeded %ds — killing hung worker (pid=%s)",
                  CYCLE_TIMEOUT_SECONDS, p.pid)
        status, payload = "timeout", []
        detail = f"cycle exceeded {CYCLE_TIMEOUT_SECONDS}s (hung, killed)"
    except Exception as e:
        log.error("scrape worker error: %s", e)
        status, payload = "err", []
        detail = repr(e)
    finally:
        # Reap a cleanly-finished worker quickly; force-kill a hung one.
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()
            p.join(timeout=5)
        if p.is_alive():
            p.kill()
            p.join()

    if status != "ok":
        return [], False, detail or status
    if n_ok == 0:
        # Worker finished but every source raised — no usable data this cycle.
        return [], False, detail or "all sources failed"
    return payload, True, ""


def run_once(seen: set, debug: bool = False) -> tuple:
    new_listings = 0
    new_matches = 0
    listings, cycle_ok, detail = scrape_all(seen, debug=debug)
    for li in listings:
        if li.uid in seen:
            continue

        if passes_filter(li):
            seen.add(li.uid)
            new_listings += 1
            log.info("MATCH %s | %s EUR | %s", li.uid, li.warm_rent, li.url)
            tg_send(format_alert(li))
            new_matches += 1
            continue

        if li.plz:
            # Known PLZ but outside target zone or over rent ceiling — definitive skip.
            seen.add(li.uid)
            new_listings += 1
            log.info("skip  %s (plz=%s rent=%s)", li.uid, li.plz, li.warm_rent)
            continue

        # PLZ still unknown after enrichment attempt. Retry until exhausted,
        # then alert (flagged) or skip silently based on ALERT_ON_UNVERIFIED.
        n = _RETRY.get(li.uid, 0) + 1
        _RETRY[li.uid] = n
        if n < MAX_DETAIL_RETRIES:
            log.info("retry-later %s (attempt %d/%d, PLZ unknown)",
                     li.uid, n, MAX_DETAIL_RETRIES)
        elif ALERT_ON_UNVERIFIED:
            seen.add(li.uid)
            new_listings += 1
            log.info("unverified alert %s after %d attempts", li.uid, n)
            tg_send(format_alert(li, unverified=True))
            new_matches += 1
        else:
            seen.add(li.uid)
            new_listings += 1
            log.info("give-up %s after %d detail attempts", li.uid, n)

    save_seen(seen)
    return new_listings, new_matches, cycle_ok, detail


def main():
    debug = "--debug" in sys.argv
    seen = load_seen()
    if not seen:
        log.warning("Seen-state is empty — all current listings will be treated as new. "
                    "If unexpected, check that SEEN_PATH points to a persistent volume.")
    log.info("Watcher start. %d known listings. ceiling=%s EUR. %d PLZ.",
             len(seen), MAX_WARM_RENT, len(ALLOWED_PLZ))
    # Log effective runtime config so the deployed values (esp. POLL_INTERVAL_SECONDS)
    # can be confirmed from the logs — this is how we verify env-var overrides applied.
    log.info("Config: poll=%ss jitter=0-%ss cycle_timeout=%ss launch_timeout=%sms "
             "max_detail_retries=%s alert_on_unverified=%s max_consecutive_failures=%s",
             POLL_INTERVAL_SECONDS, POLL_JITTER_SECONDS, CYCLE_TIMEOUT_SECONDS,
             BROWSER_LAUNCH_TIMEOUT_MS, MAX_DETAIL_RETRIES, ALERT_ON_UNVERIFIED,
             MAX_CONSECUTIVE_FAILURES)
    send_status_ping(seen, 0, 0, "Started")

    if debug:
        run_once(seen, debug=True)
        return

    daily_new = 0
    daily_matches = 0
    last_ping_date = None
    consecutive_failures = 0

    while True:
        try:
            nl, nm, cycle_ok, detail = run_once(seen)
            daily_new += nl
            daily_matches += nm
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
            cycle_ok, detail = False, repr(e)

        # Track outright scrape failures (browser crash, timeout, all sources down)
        # separately from clean-but-empty polls. This is the failure mode that
        # silently freezes the 'seen' counter, so we alert on it and, if it
        # persists, exit so Railway (restartPolicy=ALWAYS) gives us a FRESH
        # container — the escape hatch this watcher previously lacked.
        if cycle_ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            log.error("scrape cycle FAILED (%d in a row): %s",
                      consecutive_failures, detail)
            _maybe_notify_failure(consecutive_failures, detail)
            if MAX_CONSECUTIVE_FAILURES and consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("hit %d consecutive failures — exiting for a fresh "
                          "container (restartPolicy=ALWAYS will restart us).",
                          consecutive_failures)
                tg_send(f"🔁 HOWOGE watcher: {consecutive_failures} failed cycles — "
                        f"restarting the container to recover.")
                sys.exit(1)

        today = datetime.now().date()
        if datetime.now().hour >= 12 and last_ping_date != today:
            send_status_ping(seen, daily_new, daily_matches, "Daily status")
            daily_new, daily_matches, last_ping_date = 0, 0, today
        # Jitter the cadence so requests aren't a perfectly regular beat.
        time.sleep(POLL_INTERVAL_SECONDS + random.uniform(0, POLL_JITTER_SECONDS))


if __name__ == "__main__":
    main()
