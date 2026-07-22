#!/usr/bin/env python3
"""application_probe.py — application-form scout (READ-ONLY. NEVER submits).

Standalone from the watcher. It:
  1. loads the inberlinwohnen Wohnungsfinder in a real headless browser,
  2. harvests currently-listed vacancies grouped by landeseigene company
     (HOWOGE, degewo, GESOBAU, Gewobag, STADT UND LAND, WBM, berlinovo),
  3. opens each candidate exposé and decides whether the listing is CURRENT
     (the exposé echoes BOTH the PLZ and the m² size from the finder card) or
     expired (expired listings usually show a generic page, whose form is
     meaningless),
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
                                       [--target-current N=2]
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

# Phrases that mark a dead/expired/generic listing page.
_EXPIRED_PHRASES = [
    "nicht mehr verfügbar", "nicht mehr verfuegbar", "bereits vergeben",
    "leider vergeben", "leider schon vergeben", "existiert nicht",
    "abgelaufen", "nicht gefunden", "seite nicht gefunden", "404",
    "keine ergebnisse", "zurzeit keine",
]

_CONSENT_LABELS = ["Alle akzeptieren", "Alle Cookies akzeptieren", "Akzeptieren",
                   "Zustimmen", "Einverstanden", "Accept all", "Accept", "OK"]
_CONSENT_SELECTORS = [
    "[data-testid='uc-accept-all-button']",
    "button#onetrust-accept-btn-handler",
    "[aria-label*='akzeptier' i]",
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
  const captcha = /recaptcha|g-recaptcha/i.test(html) ? 'recaptcha'
                : /hcaptcha/i.test(html) ? 'hcaptcha'
                : /turnstile|cf-chl|challenge-platform/i.test(html) ? 'turnstile'
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
def harvest_candidates(page) -> list:
    """From the finder, return [{company, href, plz, size, card_text}] using the
    card each company link sits in (nearest ancestor whose text has € and m²)."""
    raw = page.evaluate(r"""
      () => {
        const host=/howoge|degewo|gesobau|gewobag|stadtundland|wbm|berlinovo/i;
        const out=[]; const seen=new Set();
        for (const a of document.querySelectorAll('a[href]')) {
          const h=a.href||''; if(!host.test(h)||seen.has(h)) continue;
          seen.add(h);
          let el=a, card=null;
          for (let i=0;i<10 && el;i++,el=el.parentElement){
            const t=el.innerText||'';
            if(/€/.test(t) && /m²|m2/i.test(t)){card=el;break;}
          }
          out.push({href:h, text:(a.innerText||'').trim().slice(0,80),
                    card:(card?card.innerText:'').trim().slice(0,400)});
        }
        return out;
      }
    """)
    cands = []
    seen = set()
    for r in raw:
        href = r.get("href", "")
        comp = _company_of(href)
        if not comp or href in seen:
            continue
        seen.add(href)
        card = r.get("card", "")
        cands.append({
            "company": comp,
            "href": href,
            "plz": _first_plz(card),
            "size": _first_size(card),
            "card_text": card,
            "is_detail": bool(_DETAIL_RE.search(href)),
        })
    # detail-looking links first, otherwise page order
    cands.sort(key=lambda x: (not x["is_detail"],))
    return cands


def classify_and_map(context, cand: dict) -> dict:
    """Open the exposé, classify current/expired (PLZ AND size must echo), and
    for current listings map the application form. GET-only, never submits."""
    url = cand["href"]
    sample = {
        "url": url,
        "captured_at": _now(),
        "expected_plz": cand.get("plz"),
        "expected_size": cand.get("size"),
        "status": "expired",
    }
    page = context.new_page()
    page.set_default_timeout(9000)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=35000)
        sample["http_status"] = resp.status if resp else None
        _dismiss_consent(page)
        page.wait_for_timeout(1500)
        scan = page.evaluate(_FORM_SCAN_JS)
        body = scan.get("body_text", "") + " " + (scan.get("title") or "")

        # --- expiry classification: CURRENT requires PLZ AND size to echo ---
        plz_ok = bool(cand.get("plz")) and cand["plz"] in body
        size_ok = _size_matches(cand.get("size"), body)
        expired_phrase = next((p for p in _EXPIRED_PHRASES if p in body.lower()), None)
        sample["plz_echoed"] = plz_ok
        sample["size_echoed"] = size_ok
        sample["expired_phrase"] = expired_phrase
        is_current = plz_ok and size_ok and not expired_phrase
        sample["status"] = "current" if is_current else "expired"

        # screenshot regardless (evidence of what we saw)
        shot = SHOTS_DIR / f"{cand['company']}_{abs(hash(url)) % 10_000_000}.png"
        try:
            page.screenshot(path=str(shot), full_page=False)
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
            print(f"[*] loading finder {FINDER}", file=sys.stderr)
            finder.goto(FINDER, wait_until="domcontentloaded", timeout=45000)
            _dismiss_consent(finder)
            finder.wait_for_timeout(3000)
            try:
                finder.mouse.wheel(0, 3000)
                finder.wait_for_timeout(1500)
            except Exception:
                pass
            cands = harvest_candidates(finder)
            finder.close()
            print(f"[*] harvested {len(cands)} company links", file=sys.stderr)

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
                      f"  (plz_echoed={sample.get('plz_echoed')},"
                      f" size_echoed={sample.get('size_echoed')})", file=sys.stderr)
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
