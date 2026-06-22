# NCAA Player Ranking Search (D1 + D2)

A self-hosted web app to **search, filter, and rank NCAA men's basketball
players** by any combination of stats using **weight sliders**, blending raw
production with **conference strength** into a single tunable composite score —
built for scouting European-import candidates across **Division I and Division
II**.

- **Backend:** Python 3.11+, FastAPI + uvicorn
- **Storage:** SQLite (`data/ncaa.db`), plain `sqlite3` (no ORM)
- **Frontend:** single page served by FastAPI — Tabulator.js table + range-input
  weight sliders + Tailwind (all via CDN, no build step)
- **Sources (free only):** Bart Torvik (D1) and the official NCAA stats portal
  `stats.ncaa.org` (D2)

---

## Quick start

```bash
cd ncaa-search
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) (Optional) Try it immediately with SYNTHETIC demo data — no network needed.
python -m ingest.seed_demo --season 2025 --clear

# 2) Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

Default landing view: **current season, Division I, Seniors**, sorted by a
balanced composite with conference strength weighted.

> **Demo data is clearly fake.** Every seeded row has `source='DEMO_SYNTHETIC'`
> and names like `D1 Demo Player 7`. It exists only to exercise the UI/scoring
> offline. Replace it with real data via the ingestion commands below.

---

## Ingesting real data

Ingestion is **idempotent** and parameterized by `--season YYYY` and division.
Raw HTTP responses are cached under `data/cache/`; pass `--refresh` to bypass.

### Division I — Bart Torvik

```bash
python -m ingest.torvik_d1 --season 2025
```

This pulls player-season advanced stats (`getadvstats.php`) and team ratings
(`trank.php`), aggregates team ratings per conference into a 0–1 strength
rating, and prints a sanity check (row counts + top senior scorers).

> ⚠️ **Verify the column mapping each season.** Torvik's CSV endpoints have **no
> header row**, so columns are mapped positionally and the layout can drift. The
> per-game counting stats (PPG/RPG/APG…) live in the tail of the row and are the
> most likely to move; they are left **unmapped (NULL) by default** until you
> confirm their indices:
>
> ```bash
> python -m ingest.torvik_d1 --season 2025 --inspect          # prints indexed columns
> python -m ingest.torvik_d1 --season 2025 --inspect-what team
> ```
>
> Then either edit `TORVIK_PLAYER_TAIL` in `ingest/torvik_d1.py`, **or** (no code
> edit) drop a `data/torvik_columns.json` override, e.g.:
>
> ```json
> { "player_tail": { "pts_pg": 60, "reb_pg": 58, "ast_pg": 56 },
>   "team": { "barthag": 18 } }
> ```

### Division II — stats.ncaa.org

```bash
python -m ingest.ncaa_d2 --season 2025
```

D2 has **no clean API**. We scrape per-category individual-statistics ranking
tables (HTML), join them by `(player, team)`, and store whatever counting
stats / shooting %s are exposed. **Advanced metrics (TS%, usage, ORtg, BPM…)
are typically unavailable for D2 and are stored as NULL** — the UI marks these
rows.

> ⚠️ **Verify the category ids each season.** The portal uses per-category
> `stat_seq` ids and an `academic_year` param that can change. They live in
> `data/ncaa_d2_categories.json` (auto-created with documented defaults). Inspect
> a category's live table structure with:
>
> ```bash
> python -m ingest.ncaa_d2 --season 2025 --inspect --category scoring
> ```

### Conference strength

- **D1:** derived automatically during `torvik_d1` ingestion (mean team
  `barthag` per conference, min-max normalized 0–1 within D1).
- **D2:** from an **editable tier CSV** — `data/d2_conference_tiers.csv`
  (tier 1–5, default **3 = neutral**). Edit it to reflect competition level,
  then rebuild:

  ```bash
  python -m ingest.conferences --division D2 --season 2025
  ```

---

## How the composite score works

1. Every numeric metric is converted to a **percentile (0–100) within its
   `(division, season)` population** — so a player's standing reflects all peers,
   not just the filtered subset.
2. The composite is a **weighted average** of the player's percentiles over the
   metrics you gave a positive weight to.
3. **Lower-is-better** metrics (turnovers, TO%, DRtg) are inverted automatically.
4. **Conference strength** is just another weightable metric (already 0–1 within
   division, scored as `value × 100`).
5. **Missing (NULL) metrics** — default `exclude`: dropped from that player's
   average and the remaining weights renormalized. Alternative `median`: treated
   as the 50th percentile. Toggle in the UI.
6. **Cross-division** comparison applies a `division_factor` (default D1 = 1.0,
   D2 = 0.85) that scales D2 composites down. This is **approximate** — keep
   divisions separate for the most reliable ranking. Adjustable in the UI.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | seasons, divisions, conferences (+strength), classes, positions, rankable metrics (with `higher_is_better`), presets |
| `GET /api/players` | ranked rows + `composite_score`, paginated |
| `GET /api/export.csv` | same filters/weights → CSV download |
| `POST /api/refresh` | trigger re-ingestion (requires `REFRESH_TOKEN`) |

`/api/players` query params: `season`, `division` (repeatable), `class`
(repeatable, default `Sr`; use `all` for every class), `conference`
(repeatable), `position`, `min_gp`, `min_minutes`, `min_conf_strength`,
`null_policy` (`exclude`|`median`), `division_factor_D1`,
`division_factor_D2`, `page`, `page_size`, `sort`, `dir`, and one
`w_<metric>=0..100` per weighted metric (e.g. `w_pts_pg=80`).

Protected refresh example:

```bash
export REFRESH_TOKEN=changeme
curl -X POST "http://localhost:8000/api/refresh?division=D1&season=2025&token=changeme"
```

---

## Frontend features

- Filters: season, division (D1/D2), class (default Senior, toggleable + "All"),
  conference multi-select (strength shown), position, min games, min minutes,
  min conference strength, NULL-handling policy, division factors.
- A **weight slider** for every rankable metric (default = a balanced preset).
- **Presets:** Balanced, Scoring big, 3-and-D wing, Floor general, Rim protector,
  Rebounder, Efficiency, plus Reset.
- **Tabulator** results table: Composite first, conference + strength, division
  badge, class; sortable by any column; row click shows the full stat line and
  flags missing advanced metrics; per-game cells show percentile on hover.
- **CSV export** and **shareable URL** (all filters + weights live in the query
  string) buttons.
- Data freshness (`updated_at`) shown in the header; `source` per row.

---

## Nightly refresh (cron)

```cron
# Refresh D1 + D2 every night at 04:30, then the D2 tiers. Logs to data/refresh.log.
30 4 * * * cd /opt/ncaa-search && /opt/ncaa-search/.venv/bin/python -m ingest.torvik_d1 --season 2025 --refresh >> data/refresh.log 2>&1
40 4 * * * cd /opt/ncaa-search && /opt/ncaa-search/.venv/bin/python -m ingest.ncaa_d2 --season 2025 --refresh >> data/refresh.log 2>&1
```

---

## Tests

```bash
python -m pytest tests/ -q
```

Covers percentile normalization (ordering, ties, NULLs, inversion) and composite
scoring (weighting, NULL policies, division factor).

---

## Honest limitations

- **D2 is sparse.** stats.ncaa.org exposes basic counting stats and shooting %s
  — few or no advanced metrics. Expect many NULLs for D2; the UI labels them.
- **D2 conference strength is coarse** — a manual 1–5 tier table you maintain,
  not a computed rating. Defaults are neutral (3).
- **Source layouts drift.** Torvik CSV column order and NCAA `stat_seq` ids may
  change between seasons; re-run the `--inspect` commands and adjust the mappings
  (no code edit needed for Torvik columns / D2 categories — use the JSON files).
- **Cross-division comparison is approximate** (see `division_factor`).
- We **do not bulk-scrape sports-reference.com** (their TOS prohibits it) and we
  **never fabricate stats** — unavailable fields are stored as NULL.
- This build environment had no egress to Torvik/NCAA, so real ingestion must be
  run where outbound HTTPS to those hosts is allowed (a normal VPS).

## Project layout

```
ncaa-search/
  app/      main.py · api.py · db.py · scoring.py
  ingest/   torvik_d1.py · ncaa_d2.py · conferences.py · seed_demo.py · common.py
  data/     cache/ · d2_conference_tiers.csv · ncaa_d2_categories.json · ncaa.db
  web/      index.html · app.js
  tests/    test_scoring.py
```
