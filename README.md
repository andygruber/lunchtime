# Lunchtime

Collects the weekly lunch menus of restaurants (usually PDFs with changing URLs behind a stable page),
stores them and shows **what's on today** for the restaurants a visitor picked.

- Restaurant sources are YAML files in `profiles/<state>/<town>/` (multi-town sources in `profiles/chains/`),
  extraction rules in `rules/` — versioned in git.
- Menus are extracted by a declarative rule first; if that fails (or there is none) an optional LLM extracts
  the menu and **proposes a new rule**, which is verified automatically and can be accepted in the admin UI.
- Visitors pick favourite restaurants, language, color scheme and display settings (favourites or all restaurants,
  sorted by town or distance, vegetarian/vegan filter); all of it lives in a single functional cookie. Nothing else.
- Docker Compose deployment for an Oracle Cloud arm64 host behind a Cloudflare Tunnel.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[llm,dev]"
.venv/bin/pytest
.venv/bin/python -m app.cli import                     # fetch all sources once
SCHEDULER_ENABLED=false .venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. For the admin UI set `ADMIN_PASSWORD_HASH` (see below) and open `/admin`.

## Configuration

Environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs the admin session cookie. Required in production. |
| `ADMIN_PASSWORD_HASH` | Argon2 hash from `python -m app.cli hash-password`. Admin UI is disabled without it. |
| `IMPORT_SCHEDULE` | Cron expressions separated by `;` (default `0 6-12 * * mon-fri; 0 18 * * sun`). A profile can override it with `schedule:`. |
| `TZ` | Time zone for "today" and the schedule (default `Europe/Vienna`). |
| `LLM_PROVIDER`, `LLM_MODEL` | `anthropic` (PDF is attached) or `openai` (any OpenAI-compatible endpoint, positioned text is sent). Empty = no LLM. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, `LLM_BASE_URL` | Credentials / endpoint for the provider. |
| `DATA_DIR`, `PROFILES_DIR`, `RULES_DIR`, `CONFIG_DIR` | Paths (defaults: `data/`, `profiles/`, `rules/`, `config/`). |

`config/site.yml` holds the site name and the **Impressum** data — replace the `[placeholders]` before going live.

## Adding a restaurant

1. Admin → **Links finden**: enter the page that links the menu PDFs. You get all PDF links, a suggested
   label regex for each and a profile template.
2. Create `profiles/<state>/<town>/<source>.yml`, e.g. `profiles/burgenland/eisenstadt/my-source.yml`
   (admin → Dateien, or in your editor). A restaurant with one location is written in the **short form**:

   ```yaml
   id: my-source
   name: Restaurant Example
   index_url: https://example.org/speiseplan
   city: Eisenstadt
   address: Hauptstraße 1, 7000 Eisenstadt
   geo: {lat: 47.846, lon: 16.524}        # python -m app.cli geocode fills this in
   phone: +43 2682 12345
   hours: [Mo–Fr 11:00–14:00, Sa & So geschlossen]
   info: Mittagsmenü € 11,90              # optional note on the card
   links:                                 # fixed links, e.g. the à la carte page
     - {title: Speisekarte, url: 'https://example.org/karte'}
   documents:
     - id: weekly
       kind: weekly                       # weekly | special (e.g. buffet flyer) | static (link only)
       target: link                       # link: a linked file | page: the HTML page itself | image: a picture
       match: {label: '^Wochenkarte'}     # regex on link text and/or `url:`; pdf_only: false for non-.pdf URLs
       rule: ghk-grid                     # rules/<id>.yml, an inline rule, extends:, or python:module:func
   ```

   Its location id is `main`, so the key used in the database and in favourites is `my-source/main`.

   Document options: `page` (look on another page than `index_url`), `selector` (CSS selector for the
   region), `index` (n-th match, e.g. the first image), `url_rewrite` (e.g. request original-size images),
   `ocr: {threshold, psm}` for pictures, `llm: false` to never use the LLM for it, and for a `special`
   `replaces_menu: true` (on its date only the special is shown, e.g. when the weekly plan just says "Buffet"),
   and `optional: true` for documents that are only online now and then (no import warning when nothing matches).

   One source can serve many locations (one shared download page): list them under `locations:` (each with
   its own `id` and `name`) and put what they share into `defaults:`. A location's own keys win, lists are
   replaced rather than merged, and `${id}` in defaults becomes the location id (see
   `profiles/chains/naglreiter.yml`). A source whose locations are in several towns goes into
   `profiles/chains/`. A location can have several documents (e.g. a weekly menu plus a `special` buffet
   flyer whose single date is taken from the PDF).

   Folders are only for finding files: ids never depend on the path, so files can be moved freely. The admin
   UI shows a hint when a location's `city` does not match its town folder (folder = town in lowercase,
   umlauts as ae/oe/ue, spaces as `-`, e.g. `neusiedl-am-see`, `guessing`).
   Menus published as **web pages** are rendered into positioned text (tables become grids) and menus
   published as **images** are read with Tesseract OCR — the same rules work on all of them.
3. Admin → document → **In Testbench öffnen**: edit the rule, test it against the archived PDF, save it as a
   rule file and assign it. Or leave `rule:` empty and let the LLM extract the menu and propose a rule
   (admin → **Regelvorschläge**, accept = write `rules/<id>.yml` and assign it).
4. **Commit** the changed files in `profiles/` and `rules/` (the admin UI writes directly into the checkout).

Dates always come from the PDF content (link labels are not trusted); documents whose week is far from the
fetch date are marked `stale` and skipped (e.g. last year's plan uploaded again).

## Websites that block automated requests

Some web hosts reject every request that is not a browser (HTTP 403, even for `robots.txt`); as of
2026-11-02 this affects die alm, Schlosscafe, Fred Sinowatz Haus and Theresianum.

- By default Lunchtime identifies as `LunchtimeBot/1.0` with a contact URL (`base_url` in
  `config/site.yml`).
- A source can opt in to ordinary browser headers with `user_agent: browser` (or set a custom User-Agent
  string). The sources above use it. Keep the request volume low (the default schedule fetches each
  page a few times per weekday morning) and drop the option if the operator allows LunchtimeBot.
- After a 401/403/429 the import log says so, the run is marked `blocked`, and scheduled imports of that
  source pause for 24 hours (manual imports from the admin UI still try).
- Fallback: admin → **Hochladen**: save the page in your browser ("Save page as… → HTML only") or copy its
  text, and upload it for the location. It is processed with the location's rule like a fetched document.
  For tables (BSH) upload the saved HTML file rather than copied text.

## Rule language

All options are defined (with descriptions) in `app/extract/rules.py`. Existing rules in `rules/` cover the
common layouts.

| Type | Layout | Key options |
|---|---|---|
| `grid` | Table with weekday columns (or rows) | `orientation`, `categories` (ordered label regexes; auto-detected if empty), `row_split: auto/top`, `column_align`, `end_marker` |
| `sections` | Weekday headings in running text | `text_columns`, `item_split: gap/line`, `lines` (regex with `category`/`name` groups), `default_category`, `end_marker` |
| `single` | Flyer for one date | `title`, `title_pattern` |

Grid extras: `weekdays: short` ("MO", "DI"), `row_split: separators` (rows from drawn table lines, e.g. in
images with unreadable row labels), `item_separator` (two dishes in one cell, e.g. `'^oder$'`).
Section extras: `day_heading` (custom heading regex, a date in it sets the day), `day_heading_inline`
(dish on the weekday line), `item_split: join` (one dish per line, wrapped lines rejoined),
`category_heading`, `weekly_heading` (dishes after it are valid all week),
`item_split: end` + `item_end` (a dish ends with its price line).

Common to all: `weekly` (regexes over the whole text for all-week dishes), `category_prices`
(prices printed once per category), `notes` (price lines etc. shown on the card), `tags` (diet markers
like `V+` → vegan), `week_split` (pages listing several weeks), `weekend`, `min_letters` (OCR debris),
`ignore`, `skip_categories` (e.g. dinner), `closed`, `special`, `date_range`, `price`,
`validate` (`min_days`, `min_items_per_day`, `max_days_from_fetch`, and `min_weekly_items`: fewer all-week
dishes than expected only give a **warning** — the menu is still shown, the import run is marked `warning` and
the document is listed on the admin dashboard until a fixed rule processes it cleanly).
Allergen codes (`A,C,G`, `ACGL`, `(A, C, G)`, `(a,c,g)`, `A/C/G`) and `€ 9,50` prices are recognised
automatically.

For layouts YAML cannot express, write a Python plugin (see `plugins/example.py`) and reference it as
`rule: python:plugins.my_plugin:parse`.

Command line test: `python -m app.cli test-rule some.pdf ghk-grid` (or a path to a rule `.yml`; HTML and
images work too). OCR needs `tesseract` with German language data — it is part of the Docker image; locally
the image-based tests are skipped without it.

Coordinates: `python -m app.cli geocode` looks up missing `geo` values via OpenStreetMap Nominatim
(one request per second) and writes them into the profiles; fallbacks to postcode/town are marked with a
comment.

## Deployment (Oracle Cloud arm64 + Cloudflare Tunnel)

1. **Cloudflare**: Zero Trust → Networks → Tunnels → *Create tunnel* (cloudflared). Copy the token.
   Add a public hostname, e.g. `lunch.example.org` → service `HTTP` → `app:8000`.
   Optional: protect `lunch.example.org/admin*` with a Cloudflare Access application (e.g. email OTP).
2. **Server** (Ubuntu or Oracle Linux on an Ampere A1 instance) with Docker Engine + Compose plugin.
   No inbound ports are needed for the tunnel — the OCI security list can stay closed except SSH.
3. Deploy:

   ```bash
   git clone <repo> lunchtime && cd lunchtime
   cp .env.example .env
   # fill in SECRET_KEY, CLOUDFLARE_TUNNEL_TOKEN, APP_UID/APP_GID (id -u / id -g), optional LLM settings
   docker compose build
   docker compose run --rm app python -m app.cli hash-password   # paste result into .env (single quotes)
   docker compose up -d
   docker compose logs -f app
   ```

   The image is built natively on the arm64 host. The scheduler runs inside the app (single worker) and
   catches up on startup if the last import is older than an hour.
4. **Updates**: `git pull && docker compose up -d --build`.
5. **Backups**: `data/` contains the SQLite database and the archived PDFs, e.g.
   `sqlite3 data/lunchtime.db ".backup data/backup.db"` plus copying `data/pdfs/`.
6. **Admin edits**: changes made in the admin UI land in `profiles/` and `rules/` of the server checkout —
   review with `git diff` and commit them.

## Privacy

The public site sets one cookie (`lunchtime`: favourites, language, color scheme, display settings, notice
acknowledged), uses no external fonts, scripts or analytics, and stores nothing about visitors on the server.
"Nearby" sorting asks the browser for the position and ranks the restaurants in the browser; the position is
kept rounded in `sessionStorage` and never sent to the server. The admin session uses
a second, admin-only cookie. Impressum and privacy policy texts live in `config/site.yml` and
`app/translations/*.yml` — have them checked for your situation.

## Languages

UI texts are in `app/translations/<lang>.yml` (German and English). Add a language by adding a file with the
same keys; it is picked up automatically (browser language first, explicit choice stored in the cookie).
