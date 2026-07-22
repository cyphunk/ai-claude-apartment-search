# berlin-landeseigene-watch (howoge-watch)

Watches Berlin's state-owned (landeseigene) housing companies for new flats and
pushes a Telegram alert the moment a listing matches your price ceiling and
postal-code list. Built to run 24/7 as a Railway background worker (also runs on
any VPS, Raspberry Pi, or laptop via Docker).

**Coverage:** the primary source is the [inberlinwohnen.de](https://www.inberlinwohnen.de/wohnungsfinder/)
Wohnungsfinder, which aggregates live vacancies from **all seven** state-owned
companies at once (berlinovo, degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND,
WBM). The direct HOWOGE scraper is kept as a **fallback**, run only when the
aggregator returns nothing (so a HOWOGE flat, which appears in both, never
double-alerts).

Filter: warm rent <= `MAX_WARM_RENT` AND postal code in `ALLOWED_PLZ`.
Both are set at the top of `howoge_watch.py`.

---

## 1. Get a Telegram bot (2 min)

1. In Telegram, open **@BotFather**, send `/newbot`, follow the prompts.
2. Copy the **token** it gives you.
3. Send your new bot any message (e.g. "hi").
4. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
5. Find `"chat":{"id": ...}`. That number is your **chat id**.

---

## 2. Put this code on GitHub

From this folder:

```bash
git remote add origin https://github.com/<you>/howoge-watch.git
git branch -M main
git push -u origin main
```

(The repo is already initialised and committed. You only create the empty repo on
GitHub first, then run the three lines above. With the GitHub CLI you can instead
do `gh repo create howoge-watch --private --source=. --push`.)

---

## 3. Deploy on Railway

1. Railway dashboard -> **New Project** -> **Deploy from GitHub repo** -> pick
   `howoge-watch`. Railway reads `railway.json` and builds the `Dockerfile`.
2. Open the service -> **Variables** -> add:
   - `HOWOGE_TG_TOKEN` = your BotFather token
   - `HOWOGE_TG_CHAT` = your chat id
3. **Volume** (so it does not re-alert old flats after each redeploy):
   service -> **Settings** -> **Volumes** -> add a volume, mount path `/data`.
   The Dockerfile already points `SEEN_PATH` at `/data/seen_listings.json`.
4. Deploy. On first boot you get a Telegram ping: "HOWOGE watcher running."

That is it. New matches arrive as Telegram messages with the listing link.

---

## Tuning / debugging

- Change price ceiling, postal codes, or poll interval at the top of
  `howoge_watch.py`, commit, push. Railway redeploys automatically.
- The startup "Started" Telegram ping is **off by default** (the container
  restarts often, so it was just noise). Pass `--ping-on-start` to re-enable it;
  the daily status ping and failure alerts are unaffected.
- If a run reports 0 listings while a site clearly shows some, the page markup
  likely changed. Run with `python3 howoge_watch.py --debug`, which writes
  `debug_howoge.html`, `debug_inberlin.html`, and `debug_inberlin_api.json`.
  Inspect the repeating listing element (or the captured JSON) and update the
  `CARD_SELECTORS` / fields marked `TUNE` in the relevant `fetch_*` function.

### One-time tuning of the inberlinwohnen source

The aggregator source (`fetch_inberlinwohnen`) was written without live access to
the site, so its card selectors / JSON field mapping are a best effort and need
one validation pass against the real page:

1. Run `python3 howoge_watch.py --debug` **on a host that can reach the site**
   (e.g. the Railway container), and grab `debug_inberlin.html` /
   `debug_inberlin_api.json`.
2. Confirm the listings JSON shape (preferred) or the repeating card element, and
   adjust `_parse_inberlin` / `CARD_SELECTORS` if needed.

Until validated the source may return `[]`, which is safe: the HOWOGE **fallback**
keeps coverage, and the failure watchdog alerts if *everything* stops returning data.

## Adding more landlords/portals later

Write a `fetch_<name>(seen, debug=False)` returning `list[Listing]`, then append it
to `PRIMARY_SOURCES` (or `FALLBACK_SOURCES`). Filtering, dedup, enrichment, Telegram
alerting, failure alerts, and self-restart are all shared, so a new source is
usually 20-30 lines. The fallback list runs only when the primary list yields
nothing.
