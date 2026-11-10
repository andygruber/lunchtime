# Lunchtime – Project Plan

Web app that collects daily lunch menus from restaurant websites (usually weekly PDFs behind a
stable index page), stores them, and shows "what's for lunch today" for a user-selected list of
restaurants.

## Decisions

| Topic | Decision |
|---|---|
| Stack | Python 3.13, FastAPI, Jinja2 templates, hand-written CSS + a little vanilla JS (no frontend build step, works without JS), APScheduler in-process |
| Storage | SQLite (SQLAlchemy, schema via `create_all`; add Alembic once the schema needs migrations) + archived PDFs in `data/` |
| Source config | One YAML file per source in `profiles/<state>/<town>/` (multi-town sources in `profiles/chains/`; single-location short form, `defaults:` for chains), reusable rules in `rules/`; admin UI edits these files directly (comment-preserving via ruamel.yaml) |
| Extraction | 1. YAML rule or Python plugin → 2. optional LLM fallback → LLM proposes a new YAML rule, verified automatically |
| LLM | Optional, pluggable: Anthropic Claude (native PDF input) and any OpenAI-compatible endpoint (OpenAI, Ollama, OpenRouter, …) |
| Admin auth | App login (password hash from env), optionally additionally Cloudflare Access on `/admin` |
| Hosting | Docker Compose on OCI arm64: `app` + `cloudflared` (Cloudflare Tunnel, no open ports) |
| End-user data | Only one functional cookie with the favourite restaurants, language, color scheme, display settings and banner-dismissed flag. No analytics, no external fonts/CDNs |

## Findings from the example sources

- deintimeout.at (Wix) and ghk-burgenland.at (TYPO3) both render PDF links in plain HTML → no headless browser needed.
- All sampled PDFs contain extractable text (no OCR), but layouts differ heavily:
  - Timeout: Canva two-column layout, day headings in caps, "Spezialität der Woche" valid all week, fixed Tagesmenü price.
  - GHK Oberwart / Eisenstadt: weekday-column grid with category rows (Menü 1/2, Vegetarisch, Dessert), allergens inline.
  - GHK Pinkafeld Mensa: grid with rotated row labels, week-long items ("Wochenteller", "Lieblingsteller") with own prices.
  - GHK Pinkafeld Buffet: single-date flyer (can be stale!), referenced from the weekly plan ("Wald und Wiese Buffet").
- GHK publishes several weeks at once (past + future) and link labels don't always match file contents
  → **dates are always taken from PDF content**, never from link label or filename.

## Configuration format

```yaml
# profiles/chains/ghk-burgenland.yml
id: ghk-burgenland
name: GHK Burgenland
index_url: https://www.ghk-burgenland.at/service-kontakt/speiseplaene
schedule: "0 6-12 * * 1-5"        # optional, falls back to global default
locations:
  - id: pinkafeld-mensa
    name: Mensa Pinkafeld
    city: Pinkafeld
    address: Steinamangerstraße 21, 7423 Pinkafeld
    documents:
      - kind: weekly                # weekly | special | static (link only, e.g. à la carte)
        match: { label: '^Pinkafeld Mensa Speiseplan' }   # label and/or url regex
        rule: ghk-grid              # rules/ghk-grid.yml, inline rule, or python:plugins.foo:parse
      - kind: special
        match: { label: 'Pinkafeld Mensa Buffet' }
        rule: buffet-flyer
```

```yaml
# rules/ghk-grid.yml
id: ghk-grid
type: grid                          # grid | sections | single
date_range: '(?P<from>\d{2}\.\d{2}\.(?:\d{4})?)\s*(?:bis|-)\s*(?P<to>\d{2}\.\d{2}\.(?:\d{4})?)'
columns: { by: weekday_headers }    # Montag … Samstag detected via word coordinates
rows:
  - { label: 'Menü 1', category: Menü 1 }
  - { label: 'Menü 2', category: Menü 2 }
  - { label: 'Dessert', category: Dessert }
allergens: '\(?([A-R](?:\s*,\s*[A-R])*)\)?$'
price: '€\s*(\d+,\d{2})'
weekly:                             # items valid for every day of the week
  - { pattern: 'Wochenteller\s+(?P<name>.+?)\s+€\s*(?P<price>[\d,]+)', category: Wochenteller }
ignore: ['Änderungen vorbehalten', 'Allergeninformationen.*']
validate: { min_days: 4, min_items_per_day: 2 }
```

Rule types:
- **grid** – weekday columns × category rows, using pdfplumber word coordinates.
- **sections** – day headings in text flow, optional column split (e.g. Timeout).
- **single** – one dated flyer → one special item (with description) on that date.
- **python plugin** – `rule: python:plugins.module:func` escape hatch for exotic layouts (hand-written only).

## Import pipeline

1. Scheduler triggers per source (default: weekdays hourly 06–12, plus Sunday 18:00; manual trigger in admin).
2. Fetch index page (httpx, polite UA, timeouts), extract links, match against location document rules.
3. Download matched PDFs; dedupe by SHA-256. Unchanged PDF → skip.
4. Extract:
   1. Run configured rule/plugin → validate (date range found, thresholds met).
   2. On failure or missing rule, and if an LLM is configured: LLM extracts to a strict JSON schema; result stored with `origin=llm`.
   3. LLM gets PDF text + word layout + rule-language schema + its own result and proposes a YAML rule. The app runs the proposal; if the output matches the LLM result, it's stored as a **rule suggestion** (else 1–2 retries with the diff). Admin can accept it with one click → written to the YAML file.
   4. Nothing works → the location shows "Menü als PDF" for that week; error visible in admin.
5. Week-long items are expanded to each day; specials are attached to their date and linked to "Buffet" entries in the weekly plan on the same day.
6. History is kept (configurable retention, default: forever).

## Data model (SQLite)

- `source`, `location` – mirrored from YAML on load (YAML is the source of truth).
- `document` – url, label, sha256, file path, fetched_at, valid_from/to, extraction origin (rule/plugin/llm), status, error.
- `menu_item` – location, document, date, category, name, description, price_cents, allergens, diet tags, is_special.
- `import_run` – per source run log.
- `rule_suggestion` – document, proposed YAML, verification result, status (open/accepted/dismissed).

## Public frontend (German, mobile-first)

- `/` → today; `/2026-11-03` → specific date (bookmarkable).
- One card per selected restaurant: dishes by category, prices, allergens, vegetarian/vegan badges, highlighted specials, link to source PDF.
- Date navigation: prev/next (skips weekends), swipe on mobile, week strip, date picker, "Heute" button. Past dates work as long as data exists.
- `/restaurants` → search by name/city, add/remove/reorder; list stored in cookie.
- Info banner about the single functional cookie (strictly necessary, §165 Abs 3 TKG 2021 → no consent needed).
- `/impressum` (§5 ECG, §25 MedienG) and `/datenschutz` (cookie, server logs, Cloudflare as processor). Content from config.
- Self-hosted fonts/assets only.

## Admin UI (`/admin`)

- Login (Argon2 hash in env), optional Cloudflare Access in front.
- Dashboard: sources, last run, status, errors; manual re-import per source/document.
- **Discover**: enter/choose an index URL → list all PDF links → assign to locations, generated match regex.
- **Rule test bench**: edit rule YAML, run against any archived PDF, see parsed result live.
- YAML editor for profiles/rules (writes files, comment-preserving), accept/dismiss LLM rule suggestions.
- Note: files are written on the server; changes have to be committed from the server checkout (or synced back).

## Repository layout

```
app/              FastAPI app: web/, admin/, importer/, rules/, llm/, models/
profiles/         one YAML per source: <state>/<town>/*.yml, chains/*.yml
rules/            reusable rule YAMLs
plugins/          optional Python parsers
tests/fixtures/   sample PDFs + expected JSON (regression tests per rule)
Dockerfile        multi-stage, multi-arch (linux/arm64 + amd64)
compose.yaml      app + cloudflared, volumes for data/ and profiles/
```

## Milestones (status 2026-11-01: 1–7 implemented, LLM path tested with a fake provider only)

1. Skeleton: project setup, Dockerfile, compose (app + cloudflared), config loading/validation (pydantic), DB + migrations.
2. Fetch & match: index fetch, link extraction, matching, PDF archive, scheduler, run log.
3. Rule engine: grid / sections / single + plugins, validation; rules for Timeout + GHK samples with fixture tests.
4. LLM fallback: provider abstraction (Claude, OpenAI-compatible), extraction, rule suggestion + verification loop.
5. Public UI: day view, navigation, search, cookie list, banner, Impressum/Datenschutz.
6. Admin UI: login, dashboard, discover, test bench, YAML editing, suggestions.
7. Ops: deployment guide for OCI arm64 + Cloudflare Tunnel, backup of `data/`, health check.

## Resolved questions

1. Impressum: operator data from `.env` (`IMPRESSUM_*`, overriding `config/site.yml`); placeholders until set.
2. Empty restaurant list: show all restaurants.
3. All GHK locations are configured initially.
4. UI language: German + English, extensible via translation files. Auto-detected from
   `Accept-Language`; an explicit choice is stored in the same functional cookie.
5. Profile changes made via the admin UI are committed manually from the server checkout (no git integration for now).

## Implementation notes

- Rule types as planned plus: `days_in_rows` grids (Podersdorf), `category_prices`, `skip_categories`
  (dinner rows are skipped for GHK), PDF-only link matching by default, stale-document detection
  (`max_days_from_fetch`, e.g. the 2025 Stoob plan linked as 2026).
- A newer file for the same location/document/week supersedes an older one only when the older one is no
  longer linked (GHK link labels are unreliable; two different files can claim the same week).
- Diet tags are only set from explicit markers in the dish text ("vegan", "(Vegan/Allergie)"), never from
  row labels — GHK reuses "Menü Vegetarisch" rows for fish and carbonara.
- Menus extracted by the LLM are marked in the UI ("automatisch per KI erkannt").
- Regression tests: `tests/test_fixtures.py` runs every configured rule against the archived PDFs.

## Extension 2026-11-02: more restaurants, web pages, images, locations

- Locations carry address, coordinates (geocoded via Nominatim), phone, opening hours and fixed links —
  ready for sorting by distance later.
- New document targets: the HTML page itself (rendered into positioned text, tables → grids) and images
  (Tesseract OCR with row-line detection). Change detection for pages uses the visible text only.
- New sources: Altes Backhaus, Karl Wirt, Gasthaus Kiss (images/OCR), die alm, Schlosscafe, Oscar's,
  BSH Fred Sinowatz Haus (web pages), Rabina's Hofpassage, Naglreiter ×6 (PDFs), Ruckendorfer (links only).
- die alm, Schlosscafe and BSH answer 403 to automated requests (as of 2026-11-02, host-level filter,
  World4You for Schlosscafe). Per-source opt-in `user_agent: browser` (used for these three), honest
  LunchtimeBot identity with contact URL otherwise, 24 h back-off after a block, manual upload (saved page
  or pasted text) in the admin UI as fallback.
