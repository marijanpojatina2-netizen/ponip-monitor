# NCAA Player Ranking Search (Division I)

A self-hosted web app to **search, filter, and rank NCAA Division I men's
basketball players** by any combination of stats using **weight sliders**,
blending raw production with **conference strength** into a single tunable
composite score — built for scouting European-import candidates.

- **Backend:** Python 3.11+, FastAPI + uvicorn
- **Storage:** SQLite (`data/ncaa.db`), plain `sqlite3` (no ORM)
- **Frontend:** single page served by FastAPI — Tabulator.js table + range-input
  weight sliders + Tailwind (all via CDN, no build step)
- **Data source:** Bart Torvik (barttorvik.com) — **free**, no API key required

> **Why no paid API?** Free Bart Torvik already exposes per-game production,
> efficiency, advanced metrics, and conference strength — everything the
> composite ranking needs for D1. Paid feeds (e.g. CollegeBasketballData.com)
> only buy a cleaner JSON contract and uptime guarantees; they are optional.

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

Default landing view: **current season, Seniors**, sorted by a balanced
composite with conference strength weighted.

> **Demo data is clearly fake.** Every seeded row has `source='DEMO_SYNTHETIC'`
> and names like `D1 Demo Player 7`. It exists only to exercise the UI/scoring
> offline. Replace it with real data via the ingestion command below.

---

## Ingesting real data

Ingestion is **idempotent** and parameterized by `--season YYYY`. Raw HTTP
responses are cached under `data/cache/`; pass `--refresh` to bypass.

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

**Conference strength** is derived automatically during ingestion: the mean
team `barthag` per conference, min-max normalized to 0–1.

> **Note:** run ingestion where outbound HTTPS to `barttorvik.com` is allowed (a
> normal VPS). Sandboxed/CI environments may block it.

---

## How the composite score works

1. Every numeric metric is converted to a **percentile (0–100) within the
   season's population** — so a player's standing reflects all peers, not just
   the filtered subset.
2. The composite is a **weighted average** of the player's percentiles over the
   metrics you gave a positive weight to.
3. **Lower-is-better** metrics (turnovers, TO%, DRtg) are inverted automatically.
4. **Conference strength** is just another weightable metric (already 0–1,
   scored as `value × 100`).
5. **Missing (NULL) metrics** — default `exclude`: dropped from that player's
   average and the remaining weights renormalized. Alternative `median`: treated
   as the 50th percentile. Toggle in the UI.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | seasons, conferences (+strength), classes, positions, rankable metrics (with `higher_is_better`), presets |
| `GET /api/players` | ranked rows + `composite_score`, paginated |
| `GET /api/export.csv` | same filters/weights → CSV download |
| `POST /api/refresh` | trigger re-ingestion (requires `REFRESH_TOKEN`) |

`/api/players` query params: `season`, `class` (repeatable, default `Sr`; use
`all` for every class), `conference` (repeatable), `position`, `min_gp`,
`min_minutes`, `min_conf_strength`, `null_policy` (`exclude`|`median`), `page`,
`page_size`, `sort`, `dir`, and one `w_<metric>=0..100` per weighted metric
(e.g. `w_pts_pg=80`).

Protected refresh example:

```bash
export REFRESH_TOKEN=changeme
curl -X POST "http://localhost:8000/api/refresh?season=2025&token=changeme"
```

---

## Frontend features

- Filters: season, class (default Senior, toggleable + "All"), conference
  multi-select (strength shown), position, min games, min minutes, min
  conference strength, NULL-handling policy.
- A **weight slider** for every rankable metric (default = a balanced preset).
- **Presets:** Balanced, Scoring big, 3-and-D wing, Floor general, Rim protector,
  Rebounder, Efficiency, plus Reset.
- **Tabulator** results table: Composite first, conference + strength, class;
  sortable by any column; row click shows the full stat line; per-game cells show
  percentile on hover.
- **CSV export** and **shareable URL** (all filters + weights live in the query
  string) buttons.
- Data freshness (`updated_at`) shown in the header; `source` per row.

---

## Nightly refresh (cron)

```cron
# Refresh D1 every night at 04:30. Logs to data/refresh.log.
30 4 * * * cd /opt/ncaa-search && /opt/ncaa-search/.venv/bin/python -m ingest.torvik_d1 --season 2025 --refresh >> data/refresh.log 2>&1
```

---

## Tests

```bash
python -m pytest tests/ -q
```

Covers percentile normalization (ordering, ties, NULLs, inversion) and composite
scoring (weighting, NULL policies).

---

## Honest limitations

- **Source layout drifts.** Torvik CSV column order may change between seasons;
  re-run `--inspect` and adjust the mapping (no code edit needed — use
  `data/torvik_columns.json`).
- We **do not bulk-scrape sports-reference.com** (their TOS prohibits it) and we
  **never fabricate stats** — unavailable fields are stored as NULL.
- **Division II is intentionally out of scope** for now. The schema keeps a
  `division` column (defaults `D1`) so D2 could be re-added later without a
  migration.

## Project layout

```
ncaa-search/
  app/      main.py · api.py · db.py · scoring.py
  ingest/   torvik_d1.py · conferences.py · seed_demo.py · common.py
  data/     cache/ · ncaa.db
  web/      index.html · app.js
  tests/    test_scoring.py
```
