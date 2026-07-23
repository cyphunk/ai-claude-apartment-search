#!/usr/bin/env python3
"""application_probe.py — application-form scout (READ-ONLY. NEVER submits).

Standalone from the watcher. It:
  1. loads the inberlinwohnen Wohnungsfinder in a real headless browser,
  2. harvests currently-listed vacancies grouped by landeseigene company
     (HOWOGE, degewo, GESOBAU, Gewobag, STADT UND LAND, WBM, berlinovo),
  3. opens each candidate exposé and decides whether the listing is CURRENT
     (the exposé still shows the listing's OWN PLZ and m² size — taken from the
     finder index and kept authoritative — so a redirect to a generic/other page
     reads as expired even if it lists other active flats),
  4. for CURRENT listings only, maps the application pathway: the apply entry
     ("bewerben"), form fields (+ required flags), login wall, captcha, document
     uploads, whether WBS is asked, and the form's submit target,
  5. accumulates results in probe/findings.json so it can be run repeatedly —
     satisfied companies are skipped, only the still-missing ones are chased.

It DOES NOT fill, click-submit, or POST any application form. All navigation is
GET (page loads) to reach and read forms. This is a scouting tool to decide
whether/how to build a real auto-applier later.

Usage:
    python3 probe/application_probe.py [--companies howoge,degewo,...]
                                       [--target-current N=2] [--max-pages N=3]
                                       [--refresh all|c1,c2] [--headful]
"""
import os
import re
import sys
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# HARD SAFETY INVARIANT
# ---------------------------------------------------------------------------
# This tool must never submit an application. There is no code path that fills a
# form and clicks a final submit; navigation is GET-only. Keep it that way.
DRY_RUN_ONLY = True

HERE = Path(__file__).resolve().parent
FINDINGS_PATH = HERE / "findings.json"
REPORT_PATH = HERE / "report.md"
SHOTS_DIR = HERE / "screenshots"

FINDER = "https://www.inberlinwohnen.de/wohnungsfinder/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The seven landeseigene companies inberlinwohnen aggregates.
COMPANIES = ["howoge", "degewo", "gesobau", "gewobag",
             "stadtundland", "wbm", "berlinovo"]
_HOST_RE = re.compile("|".join(COMPANIES), re.IGNORECASE)
_DETAIL_RE = re.compile(
    r"detail|mietangebote|properties|immo_ref|wohnung-id|t=ibw|expose|angebot",
    re.IGNORECASE)

# Phrases that mark a dead/expired/DEACTIVATED/generic listing page. Some sites
# keep the flat's own PLZ+size on a deactivated page (so the echo check passes) —
# these phrases catch that. See probe/PROBE_NOTES.md for per-company sources.
_EXPIRED_PHRASES = [
    "nicht mehr verfügbar", "nicht mehr verfuegbar", "bereits vergeben",
    "leider vergeben", "leider schon vergeben", "existiert nicht",
    "abgelaufen", "nicht gefunden", "seite nicht gefunden", "404",
    "keine ergebnisse", "zurzeit keine",
    "inserat deaktiviert",   # degewo: deactivated exposé still shows PLZ+size
]

# The seven company sites use assorted CMPs (Usercentrics — often in a shadow
# DOM, OneTrust, Cookiebot, custom). Playwright's get_by_role pierces open shadow
# roots, so a broad label list plus the well-known button IDs covers them.
_CONSENT_LABELS = ["Alle akzeptieren", "Alle Cookies akzeptieren", "Alle Cookies annehmen",
                   "Alles akzeptieren", "Akzeptieren", "Alle zulassen", "Zulassen",
                   "Zustimmen", "Einverstanden", "Ich stimme zu", "Verstanden",
                   "Accept all", "Accept All Cookies", "Accept", "Allow all", "OK"]
_CONSENT_SELECTORS = [
    "[data-testid='uc-accept-all-button']",           # Usercentrics (Playwright CSS pierces open shadow DOM)
    "button#onetrust-accept-btn-handler",             # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",   # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",          # Cookiebot (simple)
    "[aria-label*='akzeptier' i]",
    "[id*='accept-all' i]",
    "[class*='accept-all' i]",
]

# JS that reads every form on the current page plus the page-level signals.
_FORM_SCAN_JS = r"""
() => {
  const clip = (s, n) => (s || '').trim().replace(/\s+/g, ' ').slice(0, n || 80);
  const labelFor = (inp) => {
    if (inp.id) {
      const l = document.querySelector('label[for="' + CSS.escape(inp.id) + '"]');
      if (l) return clip(l.innerText, 80);
    }
    const p = inp.closest('label');
    if (p) return clip(p.innerText, 80);
    return clip(inp.getAttribute('aria-label') || inp.placeholder || inp.name, 80);
  };
  const forms = [];
  for (const f of document.querySelectorAll('form')) {
    const fields = [];
    for (const inp of f.querySelectorAll('input,select,textarea')) {
      const type = (inp.type || inp.tagName).toLowerCase();
      if (type === 'hidden') continue;
      fields.push({
        tag: inp.tagName.toLowerCase(),
        type: type,
        name: inp.name || '',
        required: !!inp.required || inp.getAttribute('aria-required') === 'true',
        label: labelFor(inp),
        options: inp.tagName.toLowerCase() === 'select'
          ? [...inp.options].map(o => clip(o.text, 40)).slice(0, 12) : undefined,
      });
    }
    forms.push({ action: f.action || '', method: (f.method || 'get').toLowerCase(),
                 field_count: fields.length, fields });
  }
  const html = document.documentElement.innerHTML;
  // Known captcha vendors, from the loaded HTML (no interaction needed). degewo's
  // inquiry form uses Friendly Captcha (frc-captcha / data-sitekey), which the
  // old recaptcha/hcaptcha/turnstile-only check missed. 'captcha?' is a
  // last-resort flag (unknown vendor) so a human still gets a heads-up.
  const captcha = /friendlycaptcha|frc-captcha/i.test(html) ? 'friendlycaptcha'
                : /g-recaptcha|grecaptcha|recaptcha\/api/i.test(html) ? 'recaptcha'
                : /hcaptcha/i.test(html) ? 'hcaptcha'
                : /turnstile|cf-chl|challenge-platform/i.test(html) ? 'turnstile'
                : /\baltcha\b/i.test(html) ? 'altcha'
                : /data-sitekey|class="[^"]*captcha|name="[^"]*captcha/i.test(html) ? 'captcha?'
                : null;
  const bodyText = document.body ? document.body.innerText : '';
  const rx = /bewerb|interess|kontakt|anfrage|anmeld|registr|login/i;
  const entries = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href],button')) {
    const t = clip(a.innerText, 60);
    const h = a.getAttribute('href') || '';
    if (!(rx.test(t) || rx.test(h))) continue;
    const key = t + '|' + (a.href || '');
    if (seen.has(key)) continue;
    seen.add(key);
    entries.push({ tag: a.tagName.toLowerCase(), text: t, href: a.href || '' });
  }
  return {
    title: document.title,
    url: location.href,
    body_text: bodyText.slice(0, 6000),
    forms: forms,
    captcha: captcha,
    requires_login: !!document.querySelector('input[type=password]'),
    file_uploads: [...document.querySelectorAll('input[type=file]')]
      .map(i => ({ name: i.name || '', accept: i.accept || '' })),
    wbs_mentioned: /\bWBS\b|wohnberechtigungsschein/i.test(bodyText),
    apply_entries: entries.slice(0, 15),
  };
}
"""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _company_of(url: str):
    m = _HOST_RE.search(url or "")
    return m.group(0).lower() if m else None


def _dismiss_consent(page):
    """Best-effort consent-banner dismissal; never raises."""
    try:
        for sel in _CONSENT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=2000)
                    page.wait_for_timeout(600)
                    return
            except Exception:
                pass
        for label in _CONSENT_LABELS:
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if btn and btn.count() > 0:
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(600)
                    return
            except Exception:
                pass
    except Exception:
        pass


def _first_plz(text: str) -> str:
    m = re.search(r"\b(1[0-4]\d{3})\b", text or "")  # Berlin PLZ start 10xxx-14xxx
    return m.group(1) if m else ""


def _first_size(text: str):
    """Return the numeric m² value found in text, e.g. 62.5, or None."""
    m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b", text or "", re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _size_matches(expected: float, page_text: str) -> bool:
    """True if `expected` m² appears in page_text, tolerant of DE/EN formatting
    and rounding (62,5 ≈ 62.5 ≈ 62,50 ≈ 63)."""
    if expected is None:
        return False
    for m in re.finditer(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b",
                         page_text or "", re.IGNORECASE):
        try:
            val = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if abs(val - expected) <= 1.0:  # within 1 m² absorbs rounding differences
            return True
    return False


_EURO_RE = re.compile(r"([\d][\d.\s]*,\d{2})\s*(?:€|EUR)", re.IGNORECASE)


def _parse_euro(text: str) -> float:
    """Parse German money like '1.234,56 €' -> 1234.56 (first match), else 0.0."""
    m = _EURO_RE.search(text or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(" ", "").replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _de_price(v: float) -> str:
    """1037.0 -> '1.037,00' (German formatting, as the finder prints it)."""
    return f"{v:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")


# --- Livewire finder payload + rendered cards -> per-listing (URL, PLZ, size) --
# The finder's rendered cards carry the company detail URL but not a reliable PLZ;
# the Livewire markers carry PLZ+size+street+price but no URL. So — exactly like
# the watcher — we read listings from the markers and join each to its UNIQUE
# rendered card (street + size + cold rent) to attach the real detail URL. That
# gives every candidate its OWN authoritative PLZ+size from the index, which we
# then look for on the exposé to decide active vs. gone. Helpers are copied (not
# imported) from howoge_watch.py so this probe stays standalone.
def _strip_tags(html_text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html_text or "").split())


def _iter_cluster_markers(obj):
    """Yield map-marker lists [lat, lon, summary, popup_html, …, id] from a
    Livewire mapData structure of unknown nesting (markers may be clustered under
    extra list levels)."""
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


def listings_from_payloads(payloads: list) -> list:
    """Every currently-listed vacancy from the Livewire markers, as dicts with the
    fields we need to (a) identify the flat on a rendered card and (b) probe its
    exposé: {plz, size (float), size_str, street, cold}."""
    out = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for comp in (payload.get("components") or []):
            effects = comp.get("effects") or {}
            for marker in _iter_cluster_markers(effects):
                popup = marker[3] or ""
                text = _strip_tags(popup) or str(marker[2])
                plz = _first_plz(text)
                size = _first_size(text)
                if not plz or size is None:
                    continue
                sm = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b", text, re.I)
                size_str = sm.group(1) if sm else ""
                # street = the popup line that is neither the summary nor the PLZ line
                parts = re.split(r"(?i)<\s*br\s*/?\s*>|</p>|<p[^>]*>|</strong>", popup)
                lines = [ln for ln in (_strip_tags(p).strip(" ,|") for p in parts) if ln]
                summary = next((l for l in lines if re.search(r"Zimmer|€|m²|m2", l, re.I)), "")
                plz_line = next((l for l in lines if _first_plz(l)), "")
                street = next((l for l in lines if l not in (summary, plz_line)), "")
                out.append({"plz": plz, "size": size, "size_str": size_str,
                            "street": street, "cold": _parse_euro(text)})
    return out


def _read_rendered_cards(page) -> list:
    """The finder renders ~10 apartment cards, each carrying the owning company's
    real detail link. Return [{href, t}] (t = normalised lowercase card text) for
    those cards; company detail links only. Copied from the watcher."""
    try:
        cards = page.evaluate(
            "() => { const host=/howoge|degewo|gesobau|gewobag|stadtundland|wbm|berlinovo/i;"
            " const det=/detail|mietangebote|properties|immo_ref|wohnung-id|t=ibw/i;"
            " const out=[]; const seen=new Set();"
            " for (const a of document.querySelectorAll('a[href]')) {"
            "   const h=a.href||''; if(!host.test(h)||!det.test(h)||seen.has(h)) continue;"
            "   seen.add(h);"
            "   let el=a, card=null;"
            "   for (let i=0;i<10 && el;i++,el=el.parentElement){"
            "     const t=el.innerText||'';"
            "     if(/€/.test(t) && /m²|m2/i.test(t)){card=el;break;} }"
            "   if(!card) card=a.closest('[wire\\\\:id]');"
            "   out.push({href:h, text:((card?card.innerText:'')||'').replace(/\\s+/g,' ')}); }"
            " return out; }") or []
    except Exception:
        return []
    return [{"href": c["href"],
             "t": re.sub(r"\s+", " ", (c.get("text") or "")).strip().lower()}
            for c in cards]


def _goto_next_page(page) -> bool:
    """Best-effort click of the finder list's 'next page' control so we can harvest
    cards beyond the first page. Returns True if something was clicked. Never
    raises. Tries Livewire pagination controls, then rel=next, then German labels."""
    selectors = [
        "[wire\\:click*='nextPage']", "[wire\\:click*='gotoPage']",
        "a[rel='next']", ".pagination a[rel='next']",
        "[aria-label*='ächst' i]", "[aria-label*='next page' i]",
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                el.click(timeout=2000)
                return True
        except Exception:
            pass
    for label in ["Weiter", "Nächste", "Nächste Seite", "Mehr anzeigen",
                  "Mehr laden", "Mehr Ergebnisse", "›", "»"]:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=label, exact=False)
                if loc and loc.count() > 0 and loc.first.is_enabled():
                    loc.first.click(timeout=2000)
                    return True
            except Exception:
                pass
    return False


def _collect_cards(finder, max_pages: int) -> list:
    """Read rendered cards across up to `max_pages` of the finder list, deduped by
    href. Falls back to scrolling when no pagination control is found (covers
    infinite-scroll layouts); stops early when a page yields no new cards."""
    all_cards = {}

    def _absorb():
        for c in _read_rendered_cards(finder):
            all_cards.setdefault(c["href"], c)

    _absorb()
    for _ in range(max(0, max_pages - 1)):
        before = len(all_cards)
        if not _goto_next_page(finder):
            try:
                finder.mouse.wheel(0, 4000)  # maybe it's infinite-scroll, not pages
            except Exception:
                pass
        finder.wait_for_timeout(1800)
        try:
            finder.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        _absorb()
        if len(all_cards) == before:
            break  # neither pagination nor scroll produced new cards
    return list(all_cards.values())


def _match_unique_card(mk: dict, cards: list):
    """Return the detail URL of the ONE rendered card that identifies this marker
    (street + size + cold rent), else None. Same conservative rule as the watcher:
    a wrong URL is never assigned — ambiguous/no match returns None."""
    street = re.sub(r"[\s,]+", " ", (mk.get("street") or "")).strip().lower()
    if len(street) < 4:
        return None
    size_str = mk.get("size_str") or ""
    size_variants = {s for s in (size_str, size_str.replace(".", ","),
                                 size_str.replace(",", ".")) if s and len(s) >= 2}
    price_variants = ({_de_price(mk["cold"]), _de_price(mk["cold"]).replace(".", "")}
                      if mk.get("cold") else set())

    def _street_size(c):
        if street not in re.sub(r"[\s,]+", " ", c["t"]):
            return False
        return not size_variants or any(s in c["t"] for s in size_variants)

    cand = [c for c in cards if _street_size(c)]
    if price_variants:
        cand = [c for c in cand if any(p in c["t"] for p in price_variants)]
    return cand[0]["href"] if len(cand) == 1 else None


# ---------------------------------------------------------------------------
# findings persistence
# ---------------------------------------------------------------------------
def load_findings() -> dict:
    if FINDINGS_PATH.exists():
        try:
            return json.loads(FINDINGS_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_findings(findings: dict):
    FINDINGS_PATH.write_text(json.dumps(findings, indent=2, ensure_ascii=False))


def _is_satisfied(entry: dict, target: int) -> bool:
    if not entry:
        return False
    current = sum(1 for s in entry.get("samples", []) if s.get("status") == "current")
    return current >= target and bool(entry.get("structure"))


# ---------------------------------------------------------------------------
# scraping
# ---------------------------------------------------------------------------
def harvest_candidates(cards: list, markers: list) -> list:
    """Join each Livewire marker to its unique rendered card so every candidate
    carries its OWN authoritative index tokens:
    [{company, href, plz, size, size_str, street}]. Markers that can't be matched
    to exactly one card are skipped (we never guess a URL)."""
    cands, seen = [], set()
    for mk in markers:
        href = _match_unique_card(mk, cards)
        if not href or href in seen:
            continue
        comp = _company_of(href)
        if not comp:
            continue
        seen.add(href)
        cands.append({"company": comp, "href": href, "plz": mk["plz"],
                      "size": mk["size"], "size_str": mk.get("size_str", ""),
                      "street": mk.get("street", "")})
    return cands


def classify_and_map(context, cand: dict) -> dict:
    """Open the exposé, classify current/expired, and for current listings map the
    application form. GET-only, never submits.

    The listing's PLZ and size come from the INDEX (the finder), and stay
    authoritative — they are what we record and display. CURRENT means the exposé
    we land on still shows THIS listing's own PLZ **and** size (with no expiry
    phrase). If either is missing, the exposé is gone / has been replaced by a
    generic or different page, so the listing is treated as no longer active — even
    though the page may show other, unrelated active flats."""
    url = cand["href"]
    sample = {"url": url, "initial_url": url, "final_url": None,
              "captured_at": _now(),
              "finder_plz": cand.get("plz"), "finder_size": cand.get("size"),
              "status": "expired"}
    page = context.new_page()
    page.set_default_timeout(9000)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=35000)
        sample["http_status"] = resp.status if resp else None
        _dismiss_consent(page)
        page.wait_for_timeout(1500)
        # Where we actually wound up after any redirects — a strong active/expired
        # signal on its own: e.g. WBM lands on a listing-specific URL when the flat
        # is live, but stays on the original path (a multi-listing page) when it's
        # gone. Captured before we follow any apply link.
        sample["final_url"] = page.url
        scan = page.evaluate(_FORM_SCAN_JS)
        body = scan.get("body_text", "") + " " + (scan.get("title") or "")

        # --- classify: does the exposé still show THIS listing's PLZ + size? ---
        plz_found = bool(cand.get("plz")) and cand["plz"] in body
        size_found = _size_matches(cand.get("size"), body)
        expired_phrase = next((p for p in _EXPIRED_PHRASES if p in body.lower()), None)
        sample["plz_found_on_expose"] = plz_found
        sample["size_found_on_expose"] = size_found
        sample["expired_phrase"] = expired_phrase
        is_current = plz_found and size_found and not expired_phrase
        sample["status"] = "current" if is_current else "expired"

        # screenshot regardless (evidence of what we saw). full_page so banners
        # below the fold (e.g. degewo's red "Inserat deaktiviert") are captured.
        shot = SHOTS_DIR / f"{cand['company']}_{abs(hash(url)) % 10_000_000}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            sample["screenshot"] = str(shot.relative_to(HERE))
        except Exception:
            pass

        if not is_current:
            return sample  # do NOT record a form off a non-current page

        # --- map the application form (current only) ---
        scan.pop("body_text", None)
        sample["exposé"] = scan
        # follow the primary apply entry via GET only (anchors, company host)
        entry = None
        for e in scan.get("apply_entries", []):
            h = e.get("href", "")
            if e["tag"] == "a" and h.startswith("http") and _company_of(h) \
                    and re.search(r"bewerb|interess|anfrage|kontakt", e["text"] + h, re.I):
                entry = e
                break
        if entry:
            sample["followed_entry"] = entry
            try:
                r2 = page.goto(entry["href"], wait_until="domcontentloaded", timeout=35000)
                sample["apply_http_status"] = r2.status if r2 else None
                _dismiss_consent(page)
                page.wait_for_timeout(1500)
                ap = page.evaluate(_FORM_SCAN_JS)
                ap.pop("body_text", None)
                sample["apply_page"] = ap
            except Exception as e:  # noqa: BLE001
                sample["apply_page_error"] = repr(e)
    except Exception as e:  # noqa: BLE001
        sample["error"] = repr(e)
    finally:
        try:
            page.close()
        except Exception:
            pass
    return sample


def _structure_from_sample(sample: dict) -> dict:
    """Distil the canonical application structure from a current sample."""
    pages = [sample.get("exposé", {}), sample.get("apply_page", {})]
    forms = [f for pg in pages for f in (pg.get("forms") or [])]
    # the "richest" form (most fields) is the likely application form
    main = max(forms, key=lambda f: f.get("field_count", 0), default=None)
    return {
        "apply_entry": (sample.get("followed_entry") or {}).get("href")
                       or (sample.get("followed_entry") or {}).get("text"),
        "requires_login": any(pg.get("requires_login") for pg in pages),
        "captcha": next((pg.get("captcha") for pg in pages if pg.get("captcha")), None),
        "wbs_required": any(pg.get("wbs_mentioned") for pg in pages),
        "file_uploads": [u for pg in pages for u in (pg.get("file_uploads") or [])],
        "fields": (main or {}).get("fields", []),
        "submit_target": {"action": (main or {}).get("action", ""),
                          "method": (main or {}).get("method", "")} if main else None,
        "form_count": len(forms),
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def write_report(findings: dict, companies: list, target: int):
    lines = ["# Application-form scout report", "",
             f"_Generated {_now()} — READ-ONLY, no submissions._", ""]
    still_needed = []
    for comp in companies:
        entry = findings.get(comp, {})
        samples = entry.get("samples", [])
        cur = [s for s in samples if s.get("status") == "current"]
        exp = [s for s in samples if s.get("status") != "current"]
        sat = _is_satisfied(entry, target)
        if not cur:
            still_needed.append(comp)
        st = entry.get("structure") or {}
        lines += [
            f"## {comp} — {'✅ satisfied' if sat else '⏳ needs a current listing'}",
            f"- samples: {len(cur)} current, {len(exp)} expired  "
            f"(target {target} current)",
        ]
        if st:
            lines += [
                f"- login required: {st.get('requires_login')}  |  "
                f"captcha: {st.get('captcha')}  |  WBS asked: {st.get('wbs_required')}",
                f"- fields: {len(st.get('fields') or [])}  |  "
                f"uploads: {len(st.get('file_uploads') or [])}  |  "
                f"submit: {(st.get('submit_target') or {}).get('method','?')} "
                f"{(st.get('submit_target') or {}).get('action','')}",
            ]
        # Per-listing detail (current AND expired) so problems are examinable by
        # hand: the PLZ+size FROM THE INDEX (authoritative), whether each was still
        # found on the exposé, and the initial link vs. the URL we landed on (a
        # redirect away is a strong active/expired signal).
        for s in samples:
            plz = s.get("finder_plz") or "?"
            sz = s.get("finder_size")
            sz = f"{sz:g}" if isinstance(sz, (int, float)) else "?"
            found = (f"plz{'✓' if s.get('plz_found_on_expose') else '✗'} "
                     f"size{'✓' if s.get('size_found_on_expose') else '✗'}")
            init = s.get("initial_url") or s.get("url") or ""
            fin = s.get("final_url") or ""
            redir = "  ← redirected" if (fin and fin != init) else ""
            lines.append(f"- **[{s.get('status')}]** PLZ {plz} · {sz} m² (from index)"
                         f"  — on exposé: {found}")
            lines.append(f"    - initial: {init}")
            lines.append(f"    - final:   {fin}{redir}")
        lines.append("")
    if still_needed:
        lines += ["## Still needed (re-run later to catch a live listing)",
                  ", ".join(still_needed), ""]
    REPORT_PATH.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Read-only application-form scout.")
    ap.add_argument("--companies", default=",".join(COMPANIES),
                    help="comma-separated subset to scout")
    ap.add_argument("--target-current", type=int, default=2,
                    help="current (non-expired) samples needed to satisfy a company")
    ap.add_argument("--refresh", default="",
                    help="'all' or comma-list of companies to re-grab & override")
    ap.add_argument("--max-pages", type=int, default=3,
                    help="how many pages of the finder card list to harvest URLs "
                         "from (markers/PLZ+size already cover all listings)")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    assert DRY_RUN_ONLY, "safety invariant disabled — refusing to run"
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    companies = [c.strip().lower() for c in args.companies.split(",") if c.strip()]
    refresh = {c.strip().lower() for c in args.refresh.split(",") if c.strip()}
    refresh_all = "all" in refresh
    target = args.target_current
    findings = load_findings()

    # which companies still need work this run?
    todo = []
    for c in companies:
        if refresh_all or c in refresh:
            findings.pop(c, None)  # override: drop prior structure/samples
            todo.append(c)
        elif not _is_satisfied(findings.get(c, {}), target):
            todo.append(c)
    if not todo:
        print("[=] all requested companies already satisfied — nothing to do.",
              file=sys.stderr)
        write_report(findings, companies, target)
        return

    print(f"[*] this run chases: {', '.join(todo)}", file=sys.stderr)

    from playwright.sync_api import sync_playwright
    # Use a pre-installed Chromium when present (some environments ship a browser
    # build that doesn't match the pinned Playwright's auto-download). Falls back
    # to Playwright's bundled browser (e.g. inside the Docker image) when unset.
    exe = os.environ.get("PROBE_CHROMIUM_PATH")
    if not exe and os.path.exists("/opt/pw-browsers/chromium"):
        exe = "/opt/pw-browsers/chromium"
    launch_kwargs = {"headless": not args.headful, "timeout": 60000}
    if exe:
        launch_kwargs["executable_path"] = exe
    # Chromium doesn't honour HTTPS_PROXY from the environment; pass it explicitly
    # so the scout works where outbound web is reached through a proxy. Unset in a
    # direct-network deploy (e.g. Railway), where no proxy is passed.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    context_kwargs = {"user_agent": UA}
    if proxy:
        context_kwargs["proxy"] = {"server": proxy}
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(**context_kwargs)
        try:
            finder = context.new_page()
            finder.set_default_timeout(9000)
            # Capture the Livewire JSON the finder fetches on load — it carries the
            # per-listing PLZ/size the rendered cards don't reliably expose.
            api_payloads = []

            def _capture(resp):
                try:
                    if resp.request.resource_type not in ("xhr", "fetch"):
                        return
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        api_payloads.append(resp.json())
                    elif "html" not in ct and "javascript" not in ct:
                        body = resp.text()
                        if body[:1].strip() in ("{", "["):
                            api_payloads.append(json.loads(body))
                except Exception:
                    pass

            finder.on("response", _capture)
            print(f"[*] loading finder {FINDER}", file=sys.stderr)
            finder.goto(FINDER, wait_until="domcontentloaded", timeout=45000)
            _dismiss_consent(finder)
            finder.wait_for_timeout(3000)
            try:
                finder.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            try:
                finder.mouse.wheel(0, 3000)
                finder.wait_for_timeout(1500)
            except Exception:
                pass
            cards = _collect_cards(finder, args.max_pages)
            markers = listings_from_payloads(api_payloads)
            cands = harvest_candidates(cards, markers)
            finder.close()
            print(f"[*] {len(markers)} Livewire listings, {len(cards)} rendered "
                  f"cards over up to {args.max_pages} page(s) -> {len(cands)} "
                  f"matched candidates (with index PLZ+size)", file=sys.stderr)

            need = {c: target - sum(1 for s in findings.get(c, {}).get("samples", [])
                                    if s.get("status") == "current")
                    for c in todo}
            for cand in cands:
                comp = cand["company"]
                if comp not in todo or need.get(comp, 0) <= 0:
                    continue
                print(f"[*] {comp}: mapping {cand['href']}", file=sys.stderr)
                sample = classify_and_map(context, cand)
                entry = findings.setdefault(comp, {"samples": []})
                entry.setdefault("samples", []).append(sample)
                entry["target_current"] = target
                if sample["status"] == "current":
                    need[comp] -= 1
                    # record/refresh the canonical structure from a current sample
                    if not entry.get("structure"):
                        entry["structure"] = _structure_from_sample(sample)
                entry["current_count"] = sum(
                    1 for s in entry["samples"] if s.get("status") == "current")
                entry["satisfied"] = _is_satisfied(entry, target)
                save_findings(findings)  # persist incrementally
                print(f"    -> {sample['status']}"
                      f"  (index PLZ {sample.get('finder_plz')} {sample.get('finder_size')}m²;"
                      f" on exposé plz={sample.get('plz_found_on_expose')}"
                      f" size={sample.get('size_found_on_expose')})", file=sys.stderr)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    save_findings(findings)
    write_report(findings, companies, target)
    left = [c for c in todo if need.get(c, 0) > 0]
    print(f"[+] wrote {FINDINGS_PATH.name} and {REPORT_PATH.name}."
          + (f" Still need a current listing for: {', '.join(left)}" if left
             else " All targeted companies satisfied."), file=sys.stderr)


if __name__ == "__main__":
    main()
