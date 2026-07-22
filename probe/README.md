# probe/ — application-form scout

`application_probe.py` is a **read-only** scouting tool for Phase-0 of a possible
auto-applier. It loads the inberlinwohnen Wohnungsfinder, picks recent listings
per state-owned company, opens each exposé, and records the **application form**
(fields, login wall, captcha, document uploads, WBS requirement, submit target)
so we can decide whether/how to automate applications later.

It **never submits** anything. There is no code path that fills a form and
clicks submit — all navigation is GET. A module-level `DRY_RUN_ONLY = True` and a
startup `assert` enforce this.

## Expiry safety

Live listings expire fast, and an expired exposé usually shows a **generic
page** whose form would be misleading. So a listing is treated as **`current`
only if its exposé echoes BOTH the PLZ and the m² size** captured from the finder
card (size matching is format-tolerant). Only `current` listings have their form
recorded as authoritative.

## Re-runnable / accumulating

Results accumulate in `findings.json`, keyed by company. A company is
**satisfied** once it has `--target-current` current samples with a captured
structure. Each run **skips satisfied companies** and only chases the ones still
missing a current listing — so you run it repeatedly (as different companies get
live vacancies) until all seven are covered.

## Usage

```bash
# chase every not-yet-satisfied company (default target: 2 current samples each)
python3 probe/application_probe.py

# only some companies, need just 1 current sample each
python3 probe/application_probe.py --companies howoge,degewo --target-current 1

# re-grab and override a stored structure (e.g. a site changed its form)
python3 probe/application_probe.py --refresh howoge      # or --refresh all
```

Outputs: `findings.json` (full state), `report.md` (summary + "still needed"
list), and `screenshots/` (one exposé screenshot per sample).

> Note: when we later store **personal** applicant data for a real submitter,
> that belongs in a **separate, gitignored** folder — not here.
