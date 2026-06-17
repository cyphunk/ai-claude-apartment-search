#!/usr/bin/env python3
"""
howoge_watch.py
Watches HOWOGE (and later other Berlin landeseigene) for new flats and pushes a
Telegram alert the second a match appears.

Designed to run 24/7 on any always-on machine (old laptop, Raspberry Pi, small VPS).

Filter logic: price (warm rent) <= MAX_WARM_RENT  AND  postal code in ALLOWED_PLZ.
Rooms are NOT filtered (price is the gate).

Architecture: each landlord is a "source" = a function returning a list[Listing].
Add a new source -> append it to SOURCES. Everything else (filter, dedup, alert)
is shared.

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
   First run sends a "watcher started" ping so you know Telegram works.

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
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

TG_TOKEN = os.environ.get("HOWOGE_TG_TOKEN", "")   # from @BotFather
TG_CHAT = os.environ.get("HOWOGE_TG_CHAT", "")     # your numeric chat id

MAX_WARM_RENT = 675          # EUR, total warm rent ceiling
POLL_INTERVAL_SECONDS = 240  # 4 min. Do NOT go below ~120s (be polite, avoid IP block)

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

SEEN_FILE = Path(os.environ.get("SEEN_PATH", "seen_listings.json"))
HEADLESS = True
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


def format_alert(li: Listing) -> str:
    rent = f"{li.warm_rent:.0f} EUR warm" if li.warm_rent else "rent ?"
    return (
        f"\U0001F3E0 <b>{html.escape(li.title or 'Wohnung')}</b>\n"
        f"{html.escape(li.address)}\n"
        f"{rent} | {html.escape(li.rooms)} Zi | {html.escape(li.size)}\n"
        f"WBS: {html.escape(li.wbs)} | {li.source}\n"
        f"{li.url}"
    )


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

PLZ_RE = re.compile(r"\b(1[0-3]\d{3})\b")          # Berlin PLZ 10xxx..13xxx
EURO_RE = re.compile(r"(\d[\d.\s]*[,]\d{2}|\d[\d.]*)\s*(?:EUR|\u20ac)")


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


def first_plz(text: str) -> str:
    m = PLZ_RE.search(text or "")
    return m.group(1) if m else ""


def passes_filter(li: Listing) -> bool:
    if li.plz and li.plz not in ALLOWED_PLZ:
        return False
    if not li.plz:
        # no PLZ detected -> let it through so nothing is missed; flagged in alert
        log.info("No PLZ for %s, passing through for manual check", li.uid)
    if li.warm_rent and li.warm_rent > MAX_WARM_RENT:
        return False
    return True


# ----------------------------------------------------------------------
# SOURCE: HOWOGE
# ----------------------------------------------------------------------

HOWOGE_SEARCH = "https://www.howoge.de/immobiliensuche/wohnungssuche.html"
HOWOGE_BASE = "https://www.howoge.de"


def fetch_howoge(debug: bool = False) -> list:
    """
    Load the HOWOGE search page in a headless browser, read the rendered listing
    cards, return Listing objects.

    NOTE ON SELECTORS: HOWOGE can change its markup. The card selector below is a
    best effort. If a run finds 0 listings while the site clearly shows some, run
    with --debug, open debug_howoge.html, find the repeating card element, and
    update CARD_SEL / the field extraction lines marked TUNE.
    """
    from playwright.sync_api import sync_playwright

    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(HOWOGE_SEARCH, wait_until="networkidle", timeout=45000)

        # Give the JS list time to populate.
        page.wait_for_timeout(3500)

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

            plz = first_plz(text_flat)
            warm = 0.0
            # Prefer a number near "warm"/"gesamt"/"brutto"
            mwarm = re.search(r"(warm|gesamt|brutto)[^0-9]{0,15}([\d.\s]*,\d{2})",
                              text_flat, re.IGNORECASE)
            if mwarm:
                warm = parse_euro(mwarm.group(2))
            if not warm:
                warm = parse_euro(text_flat)

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

        browser.close()

    log.info("HOWOGE: parsed %d listings", len(listings))
    return listings


# Register sources here. To add degewo/gewobag/etc later, write a fetch_xxx()
# returning list[Listing] and append it.
SOURCES = [fetch_howoge]


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

def run_once(seen: set, debug: bool = False) -> int:
    new_count = 0
    for src in SOURCES:
        try:
            listings = src(debug=debug)
        except Exception as e:
            log.error("Source %s failed: %s", getattr(src, "__name__", src), e)
            continue
        for li in listings:
            if li.uid in seen:
                continue
            seen.add(li.uid)           # mark seen even if filtered, so no re-eval spam
            if passes_filter(li):
                log.info("MATCH %s | %s EUR | %s", li.uid, li.warm_rent, li.url)
                tg_send(format_alert(li))
                new_count += 1
            else:
                log.info("skip  %s (plz=%s rent=%s)", li.uid, li.plz, li.warm_rent)
    save_seen(seen)
    return new_count


def main():
    debug = "--debug" in sys.argv
    seen = load_seen()
    log.info("Watcher start. %d known listings. ceiling=%s EUR. %d PLZ.",
             len(seen), MAX_WARM_RENT, len(ALLOWED_PLZ))
    tg_send("\u2705 HOWOGE watcher running. You will be pinged on new matches.")

    if debug:
        run_once(seen, debug=True)
        return

    while True:
        try:
            run_once(seen)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
