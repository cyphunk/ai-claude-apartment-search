# Probe notes & company knowledge

Persistent notes so future sessions (and humans) understand how the
application-form scout (`application_probe.py`) works and the **site-specific
quirks** we've learned, so we keep improving classification accuracy. Add to the
"Company-specific notes" and "Known … phrases" sections whenever a run surfaces
something new.

> Tip: if you want Claude to load this automatically when working in `probe/`,
> rename it to `probe/CLAUDE.md`.

---

## How the finder data works (inberlinwohnen Wohnungsfinder)

- The Wohnungsfinder is a **Livewire (Laravel)** app. On load it fetches JSON
  describing the component; listings are **map markers** nested under
  `components[].effects → … → mapData → cluster → [ marker, … ]`.
- Each marker is `[lat, lon, summary, popup_html, …, id]`; `popup_html` carries
  **street, PLZ, district, rooms, size (m²), net-cold rent**.
- The probe **captures** this via `page.on("response")` and parses it with
  `listings_from_payloads()` / `_iter_cluster_markers()`. These are the
  "N Livewire listings" in the report's Run summary.

## Two data sources, joined

| Source | Has | Missing |
|---|---|---|
| Livewire markers | authoritative PLZ + size + street + cold rent | detail URL |
| Rendered cards (~10/page) | the real company detail URL | reliable PLZ; € is CSS (absent from innerText) |

`harvest_candidates` joins **marker → unique rendered card** on
**street + size + cold-rent** (`_match_unique_card`), only on a *unique* match.
`_collect_cards` walks up to `--max-pages` (default 3) and also records, per
company, `links / detail_cards`, which — with the `matched` count — feed the
report's **Run summary** (so 0-candidate companies are diagnosable without the
console log: `links>0 & detail=0` = URL pattern not recognised by the `det` regex
in `_read_rendered_cards`; `links=0` = no live listing rendered).

### OPEN QUESTION — id-based join instead of fuzzy street+size+rent?
Marker `id` (`marker[-1]`) is inberlin's internal integer; the card link carries
the **company's** listing id (different id space). If a rendered card ever exposes
inberlin's id (a `data-*`/`wire:key`), an exact join would beat the fuzzy match.
TODO: inspect `debug_inberlin.html` + `debug_inberlin_api.json` (from
`howoge_watch.py --debug` on a networked host).

## Classification: current vs expired
PLZ + size come from the **index (marker)** and stay authoritative (what we
record/display). **current** = the exposé still shows **this listing's own PLZ AND
size** with no expiry/deactivation phrase. `final_url` (post-redirect) is recorded
as an extra signal. Exposé navigation sends `Referer: <finder>` (companies like
howoge 404 on direct hits — the finder links carry `?t=ibw`).

## Application-form selection
`_pick_application_form` scores forms and **excludes cookie-consent and
search/filter forms** (which can out-field the real form), preferring
contact/application forms (personal-detail fields, or `action`/id like
`anfrage|bewerb|kontakt|powermail|formframework|form_apartment`). If nothing
qualifies, the structure is recorded with `form_note` (report shows "⚠ no
application form identified") rather than a bogus form. `captcha`,
`requires_login`, and `wbs` scan the whole page, so they stay accurate regardless
of which form is chosen.

> Re-run gotcha: an already-**satisfied** company keeps its stored `structure`
> (it's skipped). To recompute with an improved picker, re-run with
> `--refresh <company>` (or `--refresh all`).

## Screenshots
`probe/screenshots/<company>_<hash>.png`, one per sample (current AND expired),
**full-page** (so below-the-fold banners like degewo's "Inserat deaktiviert" are
captured). Human diagnostic aid only — the code never reads them.

## Known expiry / deactivation phrases  (`_EXPIRED_PHRASES`)
`nicht mehr verfügbar`, `bereits vergeben`, `leider vergeben`, `existiert nicht`,
`abgelaufen`, `nicht gefunden`, `404`, `keine ergebnisse`, `zurzeit keine`,
**`inserat deaktiviert`** (degewo). Add new ones with the company + example URL.

## Known captcha vendors  (`_FORM_SCAN_JS`)
friendlycaptcha (`frc-captcha`/`data-sitekey`), reCAPTCHA (incl. v3), hCaptcha,
Turnstile, altcha, and a generic `captcha?` fallback for any unknown
`data-sitekey`/`*captcha*` container.

---

## Company-specific notes

### degewo
- **Deactivated listing keeps its own details.** Exposé redirects
  `…/de/properties/W…html` → `…/immosuche/details/<slug>` and still shows the
  listing's PLZ+size, but with a red **"Inserat deaktiviert"** banner → caught via
  the expiry phrase. Example: `W1150-00200-0026-0604`.
- **Application = TYPO3 form-framework inquiry form** embedded on the exposé
  (`tx_form_formframework[form_apartment_inquiry-…]`, POST), ~5 fields.
- **Captcha: Friendly Captcha** (`frc-captcha`, `data-sitekey`, EU endpoint) — so
  fully-automated submit is non-trivial (proof-of-work widget).

### gewobag
- **Captcha: Turnstile** (Cloudflare).
- The exposé has a **cookie-consent form** (`Essenziell/Statistiken/Externe
  Medien`) that out-fields the real form — the old max-fields picker grabbed it.
  Fixed by `_pick_application_form`. Consent banner is also not being dismissed;
  add gewobag's CMP accept selector to `_CONSENT_SELECTORS`/`_CONSENT_LABELS` when
  known.

### wbm
- **Real application form captured** (TYPO3 **Powermail**, POST, 16 fields):
  `ja/nein` (WBS?), `WBS gültig bis`, `WBS Zimmeranzahl`, `Einkommensgrenze…`,
  `Anrede`, `Name*`, `Vorname*`, `Strasse`, `PLZ`, `Ort`, `E-Mail*`, `Telefon`, a
  confirmation checkbox, and a **honeypot** ("Bitte dieses Feld NICHT ausfüllen!").
  **No captcha, no login, WBS required.** This is the concrete apply spec and the
  most automatable company so far.

### berlinovo
- The captured form was the **search filter** (`Wo?/max. Preis/min. Zimmer/…`),
  not the contact form — the old picker mis-fired; fixed by
  `_pick_application_form`. WBS mentioned. Needs a networked re-run
  (`--refresh berlinovo`) to capture the real contact form.

### howoge
- **All finder detail links 404 on direct access** (`…/detail/1771-…html?t=ibw` →
  `howoge.de/404`), so every sample reads expired. Trying `Referer: <finder>` on
  navigation (may be referrer-gated). If that doesn't help, howoge likely needs
  its own handling (cf. the watcher's direct `fetch_howoge` URL scheme). Confirm
  with a `curl` of one detail URL, with and without an inberlin Referer.

### gesobau / stadtundland
- First runs produced **0 candidates**; the Run summary's per-company
  `links / detail_cards / matched` now shows whether that's a `det`-regex miss or
  simply no live listings (early runs only saw their footer/homepage links →
  probably no vacancies at the time). Revisit if `links>0 & detail_cards=0`.

---

## Running / testing notes
- The sandbox where Claude runs has an **egress policy blocking these sites**; run
  the probe **locally** (or on Railway) where the network is open.
- `python probe/application_probe.py --headful` (`--max-pages N`, `--refresh …`),
  then review `probe/report.md` (Run summary + per-listing) + screenshots.
