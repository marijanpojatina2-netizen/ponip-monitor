"""Division II ingestion from the official NCAA statistics portal (stats.ncaa.org).

There is NO clean API for D2. We scrape the per-category individual-statistics
ranking tables (HTML), join them by (player, team), and store whatever counting
stats / shooting %s are exposed. D2 rows will have NULL for most advanced
metrics (ORtg, BPM, usage, TS%, etc.) — that is expected and acceptable.

VERIFY EACH SEASON
------------------
The portal uses category-specific "stat_seq" ids and an academic_year param.
These can change. The mapping lives in data/ncaa_d2_categories.json (created on
first run with documented defaults). To check the live structure of one
category:

    python -m ingest.ncaa_d2 --season 2025 --inspect --category scoring

That prints the parsed table's column headers so you can confirm/repair the
HEADER_MAP below or the categories JSON.

Etiquette: cached + rate-limited via ingest.common.fetch; real User-Agent.
We never fabricate values; missing fields are stored NULL.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

from app.db import get_conn, init_db
from ingest.common import fetch, normalize_class, to_float, utcnow_iso

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_JSON = PROJECT_ROOT / "data" / "ncaa_d2_categories.json"

BASE = "https://stats.ncaa.org/rankings/national_ranking"
SPORT_CODE = "MBB"        # men's basketball
DIVISION_ID = 2

# Default category -> stat_seq ids (VERIFY; see module docstring). stat_seq
# values are placeholders/observed historical ids and are meant to be edited in
# data/ncaa_d2_categories.json after inspection.
DEFAULT_CATEGORIES = {
    "scoring": 145,
    "rebounding": 859,
    "assists": 216,
    "steals": 215,
    "blocks": 214,
    "field_goal_pct": 148,
    "three_point_pct": 152,
    "free_throw_pct": 150,
    "turnovers": 449,
}

# Map (lowercased, fuzzy) table header text -> our schema field.
# Player/Team/conference handled separately. Counting stats are usually totals
# in NCAA tables, with a "PG"/per-game column when available; we prefer per-game.
HEADER_MAP = {
    "player": "name",
    "name": "name",
    "team": "team",
    "conference": "conference",
    "conf": "conference",
    "cl": "class",
    "class": "class",
    "yr": "class",
    "pos": "position",
    "gp": "gp",
    "games": "gp",
    "g": "gp",
    "mpg": "min_pg",
    "min/g": "min_pg",
    "ppg": "pts_pg",
    "pts/g": "pts_pg",
    "rpg": "reb_pg",
    "reb/g": "reb_pg",
    "orpg": "oreb_pg",
    "drpg": "dreb_pg",
    "apg": "ast_pg",
    "ast/g": "ast_pg",
    "stpg": "stl_pg",
    "stl/g": "stl_pg",
    "bkpg": "blk_pg",
    "blk/g": "blk_pg",
    "topg": "tov_pg",
    "to/g": "tov_pg",
    "fg%": "fg_pct",
    "3fg%": "fg3_pct",
    "3p%": "fg3_pct",
    "ft%": "ft_pct",
}

# Totals we can convert to per-game using GP if a per-game column is absent.
TOTALS_TO_PERGAME = {
    "pts": "pts_pg", "reb": "reb_pg", "oreb": "oreb_pg", "off reb": "oreb_pg",
    "dreb": "dreb_pg", "def reb": "dreb_pg", "ast": "ast_pg", "stl": "stl_pg",
    "blk": "blk_pg", "to": "tov_pg", "min": "min_pg",
}


def load_categories() -> dict:
    if CATEGORIES_JSON.exists():
        try:
            return json.loads(CATEGORIES_JSON.read_text())
        except Exception as exc:
            print(f"[ncaa_d2] bad {CATEGORIES_JSON}: {exc}; using defaults")
    CATEGORIES_JSON.write_text(json.dumps(DEFAULT_CATEGORIES, indent=2))
    print(f"[ncaa_d2] wrote default categories to {CATEGORIES_JSON} (verify stat_seq ids)")
    return dict(DEFAULT_CATEGORIES)


def _parse_tables(html: str):
    """Return list of (headers, rows) for every <table> using pandas if present,
    else a lightweight BeautifulSoup parse."""
    tables = []
    try:
        import pandas as pd  # type: ignore
        for df in pd.read_html(html):
            df.columns = [str(c) for c in df.columns]
            headers = [c.strip() for c in df.columns]
            rows = df.astype(object).where(df.notna(), None).values.tolist()
            tables.append((headers, rows))
        return tables
    except Exception:
        pass
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        return tables
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        headers = [th.get_text(strip=True) for th in trs[0].find_all(["th", "td"])]
        rows = []
        for tr in trs[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        tables.append((headers, rows))
    return tables


def _pick_data_table(tables):
    """Heuristic: the data table is the one whose headers include a player/name col
    and the most mapped columns."""
    best, best_score = None, -1
    for headers, rows in tables:
        lowered = [h.lower().strip() for h in headers]
        if not any(h in ("player", "name") for h in lowered):
            continue
        score = sum(1 for h in lowered if h in HEADER_MAP) + len(rows) / 1000.0
        if score > best_score:
            best, best_score = (headers, rows), score
    return best


def _normalize_key(name: str, team: str) -> str:
    return re.sub(r"\s+", " ", f"{name}|{team}".strip().lower())


def fetch_category(season: int, category: str, stat_seq, refresh: bool):
    params = {
        "academic_year": season,
        "division": DIVISION_ID,
        "sport_code": SPORT_CODE,
        "stat_seq": stat_seq,
    }
    html = fetch(BASE, params, refresh=refresh)
    return _parse_tables(html)


def parse_category_rows(tables) -> list[dict]:
    picked = _pick_data_table(tables)
    if not picked:
        return []
    headers, rows = picked
    lowered = [h.lower().strip() for h in headers]
    out = []
    for row in rows:
        rec: dict = {}
        for h, val in zip(lowered, row):
            field = HEADER_MAP.get(h)
            if field == "name":
                rec["name"] = str(val).strip() if val is not None else None
            elif field == "team":
                rec["team"] = str(val).strip() if val is not None else None
            elif field == "conference":
                rec["conference"] = str(val).strip() if val is not None else None
            elif field == "class":
                rec["class"] = normalize_class(val)
            elif field == "position":
                rec["position"] = str(val).strip() if val is not None else None
            elif field:
                rec[field] = to_float(val)
            elif h in TOTALS_TO_PERGAME:
                rec.setdefault("_totals", {})[TOTALS_TO_PERGAME[h]] = to_float(val)
        # Derive per-game from totals if direct per-game missing.
        gp = rec.get("gp")
        for tgt, tot in rec.pop("_totals", {}).items():
            if rec.get(tgt) is None and tot is not None and gp:
                rec[tgt] = round(tot / gp, 2)
        if rec.get("name"):
            out.append(rec)
    return out


def merge_categories(per_cat: dict[str, list[dict]], season: int) -> list[dict]:
    """Join category rows by (player, team)."""
    merged: dict[str, dict] = {}
    for cat, rows in per_cat.items():
        for rec in rows:
            key = _normalize_key(rec.get("name", ""), rec.get("team", ""))
            if not rec.get("name"):
                continue
            base = merged.setdefault(key, {
                "name": rec["name"], "team": rec.get("team"),
                "conference": rec.get("conference"), "division": "D2",
                "class": rec.get("class"), "position": rec.get("position"),
                "season": season, "source": "stats.ncaa.org",
            })
            for k, v in rec.items():
                if v is not None and base.get(k) in (None, ""):
                    base[k] = v
    return list(merged.values())


PLAYER_FIELDS = [
    "name", "team", "conference", "division", "class", "position", "season",
    "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg", "dreb_pg",
    "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg", "tov_pg",
    "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct", "ft_pct",
    "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg", "drtg",
    "bpm", "source", "updated_at",
]


def upsert_players(records: list[dict]) -> int:
    if not records:
        return 0
    now = utcnow_iso()
    placeholders = ",".join("?" for _ in PLAYER_FIELDS)
    update_cols = ",".join(
        f"{c}=excluded.{c}" for c in PLAYER_FIELDS
        if c not in ("name", "team", "season", "division")
    )
    sql = f"""
        INSERT INTO players ({",".join(PLAYER_FIELDS)})
        VALUES ({placeholders})
        ON CONFLICT(name, team, season, division) DO UPDATE SET {update_cols}
    """
    written = 0
    with get_conn() as conn:
        for rec in records:
            rec = {**rec, "updated_at": now}
            conn.execute(sql, [rec.get(f) for f in PLAYER_FIELDS])
            written += 1
        conn.commit()
    return written


def ingest(season: int, refresh: bool = False) -> None:
    init_db()
    cats = load_categories()
    per_cat: dict[str, list[dict]] = {}
    for cat, seq in cats.items():
        print(f"[ncaa_d2] fetching {cat} (stat_seq={seq}) ...")
        try:
            tables = fetch_category(season, cat, seq, refresh)
            per_cat[cat] = parse_category_rows(tables)
            print(f"   parsed {len(per_cat[cat])} rows")
        except Exception as exc:
            print(f"   [warn] {cat} failed: {exc}")
            per_cat[cat] = []
    records = merge_categories(per_cat, season)
    n = upsert_players(records)
    print(f"[ncaa_d2] wrote {n} D2 player rows for {season}")
    # Build D2 conference strength from the manual tiers CSV.
    from ingest.conferences import build_d2_conferences
    build_d2_conferences(season)


def inspect(season: int, category: str, refresh: bool) -> None:
    cats = load_categories()
    seq = cats.get(category, DEFAULT_CATEGORIES.get(category))
    print(f"[inspect] category={category} stat_seq={seq}")
    tables = fetch_category(season, category, seq, refresh)
    print(f"[inspect] found {len(tables)} table(s)")
    for i, (headers, rows) in enumerate(tables):
        print(f"  table {i}: {len(rows)} rows; headers={headers}")


def main():
    ap = argparse.ArgumentParser(description="NCAA D2 ingestion (stats.ncaa.org)")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--category", default="scoring")
    args = ap.parse_args()
    if args.inspect:
        inspect(args.season, args.category, args.refresh)
        return
    ingest(args.season, args.refresh)


if __name__ == "__main__":
    main()
