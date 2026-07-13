#!/usr/bin/env python3
"""
howoge_watch.py
Watches Berlin's landeseigene (state-owned) housing companies for new flats and
pushes a Telegram alert the second a match appears.

Coverage: the primary source is the inberlinwohnen.de Wohnungsfinder, which
aggregates live vacancies from ALL SEVEN state-owned companies at once (berlinovo,
degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM). The direct HOWOGE scraper
is kept as a FALLBACK, run only when the aggregator yields nothing.

Designed to run 24/7 on any always-on machine (old laptop, Raspberry Pi, small VPS).

Filter logic: price (warm rent) <= MAX_WARM_RENT  AND  postal code in ALLOWED_PLZ.
Rooms are NOT filtered (price is the gate).

Architecture: each landlord/portal is a "source" = a function returning a
list[Listing], signature (seen: set, debug: bool). Sources are split into
PRIMARY_SOURCES and FALLBACK_SOURCES; _scrape_worker runs the fallback only if the
primary produced no listings, so a flat present in both never double-alerts.
Everything else (filter, dedup, enrichment, alerting, self-restart) is shared.

Why a headless browser: both the HOWOGE search and the inberlinwohnen Wohnungsfinder
render listings with JavaScript (and sit behind a WAF), so a plain requests.get()
returns an empty/blocked page. Playwright loads the page like a real browser, reads
the rendered cards, and can also capture the listings JSON the app fetches.

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
   -> dumps rendered HTML (debug_howoge.html, debug_inberlin.html) plus the
      captured listings JSON (debug_inberlin_api.json) so the card structure /
      JSON field names can be re-checked and the selectors tuned.
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
from urllib.parse import quote_plus

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


def maps_url(li: Listing) -> str:
    """Google Maps search link for a listing's address. Cleans the query: strips any
    'rooms | size | price' prefix, ensures the PLZ and 'Berlin' are present so the
    pin lands on the right street rather than a same-named road elsewhere."""
    q = li.address or ""
    if "€" in q:                       # drop a leading "N Zimmer | X m² | Y €" summary
        q = q.split("€", 1)[-1]
    q = q.strip(" |,-")
    if li.plz and li.plz not in q:
        q = f"{q} {li.plz}"
    if "berlin" not in q.lower():
        q = f"{q}, Berlin"
    q = " ".join(q.split())
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)


def format_alert(li: Listing, unverified: bool = False) -> str:
    rent = f"{li.warm_rent:.0f} EUR warm" if li.warm_rent else "rent ?"
    head = ""
    if unverified:
        head = ("⚠️ UNVERIFIED: detail page could not be read, "
                "postal code/rent not confirmed. Check the link.\n")
    # Address as a Google Maps link (HTML mode); plain text if no address.
    if li.address:
        addr_line = (f'<a href="{html.escape(maps_url(li), quote=True)}">'
                     f'{html.escape(li.address)}</a>')
    else:
        addr_line = html.escape(li.address)
    return (
        head
        + f"\U0001F3E0 <b>{html.escape(li.title or 'Wohnung')}</b>\n"
        + f"{addr_line}\n"
        + f"{rent} | {html.escape(li.rooms)} Zi | {html.escape(li.size)}\n"
        + f"WBS: {html.escape(li.wbs)} | {li.source}\n"
        + f"{li.url}"
    )


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

PLZ_RE = re.compile(r"\b(1[0-3]\d{3}|14[01]\d{2})\b")   # Berlin PLZ 10115..14199
# NB: Berlin runs up to 14199 (Charlottenburg/Zehlendorf/Spandau). The old
# 10xxx-13xxx pattern left 14xxx unparsed, so those listings had an empty PLZ and
# slipped past the postal-code gate (which only rejects a KNOWN out-of-area PLZ).
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
        ms = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*m", text_flat)
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


# ----------------------------------------------------------------------
# SOURCE: inberlinwohnen.de — aggregator for ALL SEVEN landeseigene companies
# (berlinovo, degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM).
# One poll of the Wohnungsfinder returns every state-owned vacancy at once, so
# this single source replaces what would otherwise be seven separate scrapers.
# ----------------------------------------------------------------------

INBERLIN_FINDER = "https://www.inberlinwohnen.de/wohnungsfinder/"
INBERLIN_BASE   = "https://www.inberlinwohnen.de"


def _warm_rent_strict(text: str) -> float:
    """Warm rent ONLY when an explicit warm-rent label is present. Unlike
    extract_warm_rent(), this does NOT fall back to 'first euro in the text' —
    aggregator card/JSON text is full of other numbers (cold rent, deposit,
    fees), so a loose grab would mis-set the rent the price filter gates on.
    Returns 0.0 when warm rent isn't explicit, which routes the listing through
    the same detail-page enrichment HOWOGE uses to confirm the real warm rent."""
    m = WARM_RE.search(strip_rent_noise(text or ""))
    return parse_euro(m.group(2)) if m else 0.0


def _listing_from_text(text_flat: str, url: str, source_label: str) -> "Listing":
    """Build a Listing from a blob of listing text + its detail URL, reusing the
    same battle-tested regex extractors as the HOWOGE parser. Schema-agnostic on
    purpose: works whether the text came from a rendered card or a stringified
    JSON record, so it survives the aggregator changing its exact markup."""
    plz  = first_plz(text_flat)
    warm = _warm_rent_strict(text_flat)

    rooms = ""
    mr = re.search(r"(\d(?:[.,]\d)?)\s*Zimmer", text_flat, re.IGNORECASE)
    if mr:
        rooms = mr.group(1)
    size = ""
    ms = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*m", text_flat)
    if ms:
        size = ms.group(1) + " m2"
    wbs = "ja" if re.search(r"\bWBS\b", text_flat, re.IGNORECASE) else "?"

    # Stable id from the detail URL (works across all seven company domains); fall
    # back to a hash so a listing without a clean slug still dedups consistently.
    slug = ""
    mslug = re.search(r"([\w-]{4,})(?:\.html?)?$", (url or "").split("?")[0].rstrip("/"))
    if mslug:
        slug = mslug.group(1)
    if not slug:
        slug = str(abs(hash(url or text_flat)) % (10 ** 12))
    uid = f"inberlin:{slug}"

    return Listing(
        uid=uid, source=source_label, title=(text_flat[:60] or "Landeseigene Wohnung"),
        address=(plz or "") + " " + text_flat[:80],
        plz=plz, rooms=rooms, size=size, warm_rent=warm, wbs=wbs, url=url or INBERLIN_FINDER,
    )


def _iter_listing_dicts(payload):
    """Yield apartment-like dicts from a decoded JSON payload of unknown shape:
    the API may return a bare list, or wrap it under a key like data/results/
    items/wohnungen/immos. Best-effort and defensive."""
    if isinstance(payload, list):
        for x in payload:
            if isinstance(x, dict):
                yield x
    elif isinstance(payload, dict):
        for key in ("data", "results", "items", "wohnungen", "immos",
                    "objekte", "properties", "entries", "hits"):
            v = payload.get(key)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, dict):
                        yield x


def _url_from_dict(d: dict) -> str:
    """Find the detail-page link inside an apartment record of unknown schema."""
    for k in ("url", "link", "detailUrl", "detail_url", "permalink", "href", "expose"):
        v = d.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    # otherwise: any http-looking string value in the record
    for v in d.values():
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def _dismiss_consent(page) -> None:
    """Best-effort dismissal of a cookie/consent banner (Usercentrics/Cookiebot/
    custom), which otherwise overlays and blocks the listings from rendering. Tries
    common German accept-button labels and known CMP selectors; never raises."""
    labels = ["Alle akzeptieren", "Alle Cookies akzeptieren", "Akzeptieren",
              "Zustimmen", "Einverstanden", "Accept all", "Accept", "OK"]
    selectors = [
        "#usercentrics-root",  # shadow-DOM CMP; handled via role/text below too
        "[data-testid='uc-accept-all-button']",
        "button#onetrust-accept-btn-handler",
        "[aria-label*='akzeptier' i]",
    ]
    try:
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=2000)
                    page.wait_for_timeout(800)
                    return
            except Exception:
                pass
        for label in labels:
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if btn and btn.count() > 0:
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(800)
                    return
            except Exception:
                pass
    except Exception:
        pass


def fetch_inberlinwohnen(seen: set, debug: bool = False) -> list:
    """Load the inberlinwohnen Wohnungsfinder in a headless browser and read every
    listing from the Livewire map payload it fetches on load. The finder is a
    Livewire (Laravel) app whose POST /livewire/update response carries a
    mapData.cluster array — one marker per vacancy, each with rooms/size/price/
    street/PLZ/district — so a single response yields all listings (no pagination,
    no per-card DOM). The site's WAF blocks plain requests, hence the real browser;
    a cookie-consent banner is dismissed first so the payload loads.

    _listings_from_livewire() is the primary parser; a generic JSON-dict scan and a
    rendered-card reader are kept as fallbacks. If nothing parses, a diagnostic
    snapshot is written (see _dump_inberlin_diagnostics) and the source returns [] —
    which is safe: the HOWOGE fallback source keeps coverage and nothing
    double-alerts.
    """
    from playwright.sync_api import sync_playwright

    listings = []
    api_payloads = []  # (url, decoded_json) captured from XHR/fetch JSON responses
    net_log = []       # (resource_type, status, content_type, url) for diagnostics
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, timeout=BROWSER_LAUNCH_TIMEOUT_MS)
        try:
            page = browser.new_page(user_agent=USER_AGENT)

            def _capture(resp):
                try:
                    rt = resp.request.resource_type
                    ct = resp.headers.get("content-type", "")
                    if rt in ("xhr", "fetch", "document"):
                        net_log.append((rt, resp.status, ct.split(";")[0], resp.url))
                    # Capture JSON even when the server mislabels the content-type:
                    # try the declared JSON first, else best-effort parse the body.
                    if rt in ("xhr", "fetch"):
                        if "json" in ct:
                            api_payloads.append((resp.url, resp.json()))
                        elif "html" not in ct and "javascript" not in ct:
                            body = resp.text()
                            if body[:1].strip() in ("{", "["):
                                api_payloads.append((resp.url, json.loads(body)))
                except Exception:
                    pass

            page.on("response", _capture)
            response = page.goto(INBERLIN_FINDER, wait_until="domcontentloaded", timeout=30000)

            # German sites gate content behind a cookie-consent banner; dismiss it
            # so the listings actually render, then let lazy content load.
            _dismiss_consent(page)

            # The Livewire app fetches listings after load; give it a moment and
            # wait for either its JSON or rendered cards to arrive.
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            try:
                page.mouse.wheel(0, 3000)  # nudge any lazy/infinite-scroll rendering
                page.wait_for_timeout(1500)
            except Exception:
                pass
            try:
                page.wait_for_selector(
                    "[class*='wohnung'],[class*='result'],article,li a[href*='http']",
                    timeout=10000)
            except Exception:
                if not api_payloads:
                    diagnose_block(page, response)

            listings = _parse_inberlin(page, browser, seen, api_payloads, debug, net_log)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    log.info("inberlinwohnen: parsed %d listings", len(listings))
    return listings


_COMPANY_HOSTS = ("howoge", "degewo", "gesobau", "gewobag",
                  "stadtundland", "stadt-und-land", "wbm", "berlinovo")


def _resolve_deep_link(page, fid) -> str:
    """Resolve a per-flat deep link for listing <fid>. Navigation on the finder is
    the Livewire server method findApartmentItem(id), which opens a detail overlay;
    that overlay carries the real link (a shareable finder URL and/or an outbound
    link to the owning company's application page). We invoke the method via the
    Livewire JS API, then look for (a) the browser URL turning into a deep link, or
    (b) an outbound company/expose anchor in the overlay. Returns '' if none found
    (caller keeps the generic finder URL). Best-effort; never raises."""
    try:
        page.evaluate(
            """(id) => {
                if (!window.Livewire) return;
                const comps = (window.Livewire.all && window.Livewire.all()) || [];
                for (const c of comps) {
                    try { c.call('findApartmentItem', id); } catch (e) {}
                }
            }""", fid)
    except Exception as e:
        log.info("inberlin deep-link: Livewire call failed for %s: %s", fid, e)
        return ""
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(700)
    except Exception:
        pass

    # (a) URL became a shareable deep link — must reference THIS flat id so a stale
    # overlay URL from a previous resolve is never mis-assigned to another listing.
    try:
        url = page.url
        if (url and url.rstrip("/") != INBERLIN_FINDER.rstrip("/")
                and str(fid) in url
                and re.search(r"(wohnung|flat|expose|angebot|id=|/\d{3,})", url, re.IGNORECASE)):
            return url
    except Exception:
        pass

    # (b) outbound "apply / view offer" anchor in the opened overlay
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)") or []
    except Exception:
        hrefs = []
    # A detail link points at a specific flat, not a company homepage/footer link,
    # so require a detail-ish path or an id in it.
    detailish = re.compile(r"(expose|detail|immobil|angebot|wohnung|objekt|/\d{3,})",
                           re.IGNORECASE)
    for h in hrefs:
        hl = (h or "").lower()
        if any(c in hl for c in _COMPANY_HOSTS) and detailish.search(hl):
            return h
    for h in hrefs:
        hl = (h or "").lower()
        if "inberlinwohnen.de" in hl and detailish.search(hl) and hl.rstrip("/") != INBERLIN_FINDER.rstrip("/"):
            return h
    return ""


def _iter_cluster_markers(obj):
    """Yield map-marker lists from a Livewire mapData structure of unknown nesting.
    Each marker is [lat, lon, summary, popup_html, id] — the shape the finder emits
    via effects.dispatches[].params.mapData.cluster. We walk defensively because the
    markers may be grouped/clustered under extra list levels."""
    if isinstance(obj, list):
        if (len(obj) >= 5 and isinstance(obj[-1], int)
                and isinstance(obj[3], str)
                and re.search(r"Zimmer|€|m²|m2", f"{obj[2]} {obj[3]}", re.IGNORECASE)):
            yield obj
        else:
            for x in obj:
                yield from _iter_cluster_markers(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_cluster_markers(v)


def _strip_tags(html_text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html_text or "").split())


def _listings_from_livewire(api_payloads: list) -> list:
    """Primary parser: pull every listing from the Livewire map-data payload the
    Wohnungsfinder fetches on load. One response carries all listings as structured
    markers, so there's no pagination or per-card DOM scraping. Each marker's popup
    HTML has rooms, size, price, street, PLZ and district — everything the price/PLZ
    filter needs. The finder shows a single headline rent with no explicit Warm/Kalt
    label; we take it as the gate rent (see _warm_rent_strict note in the report)."""
    out, seen_ids = [], set()
    for _url, payload in api_payloads:
        if not isinstance(payload, dict):
            continue
        for comp in (payload.get("components") or []):
            effects = comp.get("effects") or {}
            for marker in _iter_cluster_markers(effects):
                fid = marker[-1]
                popup = marker[3] or ""
                text = _strip_tags(popup) or str(marker[2])
                plz = first_plz(text)
                price = parse_euro(text)  # single headline rent (no Warm/Kalt label)

                rooms = ""
                mr = re.search(r"(\d(?:[.,]\d)?)\s*Zimmer", text, re.IGNORECASE)
                if mr:
                    rooms = mr.group(1)
                size = ""
                ms = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*m", text)
                if ms:
                    size = ms.group(1) + " m2"
                wbs = "ja" if re.search(r"\bWBS\b", text, re.IGNORECASE) else "?"

                # Split the popup into its visual lines (summary / street / PLZ+district)
                # so the stored address is a clean street line, not the whole blob —
                # this is what the Google Maps link in the alert keys off.
                parts = re.split(r"(?i)<\s*br\s*/?\s*>|</p>|<p[^>]*>|</strong>", popup)
                lines = [ln for ln in (_strip_tags(p).strip(" ,|") for p in parts) if ln]
                summary = next((l for l in lines if re.search(r"Zimmer|€|m²|m2", l, re.I)), "")
                plz_line = next((l for l in lines if first_plz(l)), "")
                street = next((l for l in lines if l not in (summary, plz_line)), "")
                district = plz_line.replace(plz, "").strip(" ,") if (plz and plz_line) else ""
                loc = " ".join(x for x in (plz, district) if x)
                address = ", ".join(x for x in (street, loc) if x) or text[:100]

                uid = f"inberlin:{fid}"
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                out.append(Listing(
                    uid=uid, source="inBerlin",
                    title=(summary or text[:60] or "Landeseigene Wohnung"),
                    address=address, plz=plz, rooms=rooms, size=size,
                    warm_rent=price, wbs=wbs, url=INBERLIN_FINDER,
                ))
    return out


def _parse_inberlin(page, browser, seen: set, api_payloads: list, debug: bool,
                    net_log: list = None) -> list:
    """Build listings from the captured API JSON if available, else from rendered
    DOM cards. Split out of fetch_inberlinwohnen so the browser teardown lives in
    a single finally block (same shape as _parse_and_enrich)."""
    if debug:
        try:
            Path("debug_inberlin.html").write_text(page.content(), encoding="utf-8")
            Path("debug_inberlin_api.json").write_text(
                json.dumps([{"url": u, "json": j} for u, j in api_payloads],
                           ensure_ascii=False, indent=2)[:2_000_000],
                encoding="utf-8")
            log.info("Wrote debug_inberlin.html and debug_inberlin_api.json "
                     "(%d API payloads captured)", len(api_payloads))
        except Exception as e:
            log.warning("inberlin debug dump failed: %s", e)

    # --- Preferred path: the Livewire map-data payload (all listings, structured) ---
    listings = _listings_from_livewire(api_payloads)
    seen_ids = {li.uid for li in listings}

    # --- Secondary path: any other listings-like JSON dicts the app fetched ---
    if not listings:
        for _url, payload in api_payloads:
            for d in _iter_listing_dicts(payload):
                url = _url_from_dict(d)
                # A record is "apartment-like" only if its text carries a Berlin PLZ
                # or a rooms/size signal — filters out unrelated JSON (menus, config).
                blob = json.dumps(d, ensure_ascii=False)
                if not (first_plz(blob) or re.search(r"\d\s*Zimmer|\d{2,3}\s*m", blob, re.I)):
                    continue
                li = _listing_from_text(" ".join(blob.split()), url, "inBerlin")
                if li.uid in seen_ids:
                    continue
                seen_ids.add(li.uid)
                listings.append(li)

    # --- Fallback path: rendered cards (only if JSON yielded nothing) ---
    if not listings:
        # TUNE: candidate selectors for a listing card/link on the finder page.
        CARD_SELECTORS = [
            "a[href*='/wohnungssuche/']",
            "[class*='wohnung'] a[href*='http']",
            "[class*='result'] a[href*='http']",
            "article a[href*='http']",
        ]
        cards = []
        for sel in CARD_SELECTORS:
            cards = page.query_selector_all(sel)
            if cards:
                log.info("inberlinwohnen: matched %d nodes with '%s'", len(cards), sel)
                break
        for c in cards:
            href = c.get_attribute("href") or ""
            if not href.startswith("http"):
                href = INBERLIN_BASE + href
            block = c
            try:
                parent = c.evaluate_handle("el => el.closest('article, li, div')")
                el = parent.as_element() if parent else None
                if el:
                    block = el
            except Exception:
                pass
            text_flat = " ".join(((block.inner_text() if block else c.inner_text()) or "").split())
            if not (first_plz(text_flat) or re.search(r"\d\s*Zimmer|\d{2,3}\s*m", text_flat, re.I)):
                continue
            li = _listing_from_text(text_flat, href, "inBerlin")
            if li.uid in seen_ids:
                continue
            seen_ids.add(li.uid)
            listings.append(li)

    # DIAGNOSTIC: if we found nothing, snapshot what the container actually got so
    # the parser can be tuned against the real page (the site is unreachable from
    # the dev sandbox). Writes to the persistent /data volume (readable out-of-band)
    # and logs the network map. Overwrites a single file, so no unbounded growth.
    if not listings:
        _dump_inberlin_diagnostics(page, api_payloads, net_log or [])

    # Resolve a per-flat deep link for NEW listings that pass the filter (i.e. the
    # ones we're about to alert on) — a handful per cycle at most. Livewire map
    # listings otherwise point at the generic finder page. Kept to matches only so
    # the extra Livewire round-trips stay cheap and within the cycle budget.
    probed = False
    for li in listings:
        # One-time verification probe per cycle: resolve+log the first listing's
        # deep link even if already seen, so the resolver can be confirmed from logs.
        if not probed:
            probed = True
            fid = li.uid.split(":", 1)[-1]
            link = _resolve_deep_link(page, fid)
            log.info("inberlin deep-link probe: %s -> %s", li.uid, link or "(none)")
            if li.uid not in seen and passes_filter(li) and link:
                li.url = link
            continue
        if li.uid in seen or not passes_filter(li):
            continue
        fid = li.uid.split(":", 1)[-1]
        link = _resolve_deep_link(page, fid)
        if link:
            log.info("inberlin deep-link: %s -> %s", li.uid, link)
            li.url = link

    return listings


def _dump_inberlin_diagnostics(page, api_payloads: list, net_log: list) -> None:
    """Best-effort snapshot to help tune the aggregator parser: the rendered HTML
    and captured JSON go to the /data volume; the XHR/fetch endpoint map is logged
    (the log is enough to spot a listings API; the HTML is there for selectors)."""
    try:
        xhrs = [n for n in net_log if n[0] in ("xhr", "fetch")]
        log.info("inberlin DIAG: %d xhr/fetch, %d json payloads captured",
                 len(xhrs), len(api_payloads))
        for rt, status, ct, url in xhrs[:25]:
            log.info("inberlin DIAG net: [%s %s] %s %s", rt, status, ct, url[:160])
        for u, j in api_payloads[:3]:
            sample = json.dumps(j, ensure_ascii=False)[:300]
            log.info("inberlin DIAG json %s -> %s", u[:120], sample)
    except Exception as e:
        log.warning("inberlin DIAG logging failed: %s", e)
    try:
        out_dir = SEEN_FILE.parent
        (out_dir / "inberlin_debug.html").write_text(page.content(), encoding="utf-8")
        (out_dir / "inberlin_debug_net.json").write_text(
            json.dumps({
                "net": [{"type": rt, "status": st, "ct": ct, "url": u}
                        for rt, st, ct, u in net_log],
                "json_payloads": [{"url": u, "sample": j} for u, j in api_payloads],
            }, ensure_ascii=False)[:3_000_000],
            encoding="utf-8")
        log.info("inberlin DIAG: wrote %s/inberlin_debug.html + inberlin_debug_net.json",
                 out_dir)
    except Exception as e:
        log.warning("inberlin DIAG file dump failed: %s", e)


# ----------------------------------------------------------------------
# SOURCE REGISTRATION
# ----------------------------------------------------------------------
# PRIMARY = the aggregator (covers all seven landeseigene incl. HOWOGE).
# FALLBACK = the direct HOWOGE scraper, run ONLY when the primary yields nothing
# (aggregator down, markup changed, or still being tuned). Because a healthy
# aggregator always returns listings, the fallback stays dormant in steady state,
# so HOWOGE flats — which appear in both — never double-alert.
# To add another aggregator/company later, append its fetch_xxx to PRIMARY_SOURCES.
PRIMARY_SOURCES  = [fetch_inberlinwohnen]
FALLBACK_SOURCES = [fetch_howoge]

# Back-compat alias: some tooling/tests import SOURCES. It is the full set that
# *can* run; orchestration (primary-then-fallback) lives in _scrape_worker.
SOURCES = PRIMARY_SOURCES + FALLBACK_SOURCES


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

_RETRY: dict = {}  # uid -> number of inconclusive PLZ-unknown attempts


def _run_sources(sources, seen_snapshot, debug):
    """Run a list of sources, returning (listings, n_ok, last_error). n_ok counts
    sources that returned without raising (an empty list still counts as healthy)."""
    out, n_ok, detail = [], 0, ""
    for src in sources:
        try:
            out.extend(src(seen_snapshot, debug=debug))
            n_ok += 1
        except Exception as e:
            detail = f"{getattr(src, '__name__', src)}: {e}"
            log.error("Source %s failed: %s", getattr(src, "__name__", src), e)
    return out, n_ok, detail


def _scrape_worker(q, seen_snapshot, debug):
    """Run the primary sources, then the fallback sources only if the primaries
    produced NO listings (down, empty, or still being tuned). Puts
    (status, payload, n_ok, detail) on the queue. Runs in a CHILD process so a
    hung Playwright/Chromium call cannot wedge the watcher — the parent kills this
    process if it overruns CYCLE_TIMEOUT_SECONDS, and the child's exit reclaims all
    browser memory regardless of leaks.

    n_ok = how many sources returned without raising, across primary + any fallback
    run. detail = last source error, so the parent can tell a real outage (every
    source raised) apart from a clean-but-empty poll. Running the fallback only
    when the primary is empty keeps HOWOGE (present in both) from double-alerting."""
    try:
        out, n_ok, detail = _run_sources(PRIMARY_SOURCES, seen_snapshot, debug)
        if not out:
            log.info("primary sources yielded 0 listings — running fallback %s",
                     [getattr(s, "__name__", s) for s in FALLBACK_SOURCES])
            fb_out, fb_ok, fb_detail = _run_sources(FALLBACK_SOURCES, seen_snapshot, debug)
            out.extend(fb_out)
            n_ok += fb_ok
            detail = fb_detail or detail
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
