"""Seed the database with CLEARLY-LABELED SYNTHETIC demo data.

This exists ONLY so the scoring/API/frontend can be exercised end-to-end without
live network access to Torvik. Every row is tagged source='DEMO_SYNTHETIC' and
the player names are obviously fake ("D1 Demo Player 7"). These are NOT real
statistics and must never be presented as such.

Usage:
    python -m ingest.seed_demo --season 2025
    python -m ingest.seed_demo --season 2025 --clear   # wipe demo rows first
"""
from __future__ import annotations

import argparse
import random

from app.db import get_conn, init_db
from ingest.conferences import upsert_conferences
from ingest.common import utcnow_iso

D1_CONFS = ["Big Ten", "SEC", "Big 12", "ACC", "Big East", "Mountain West",
            "WCC", "American", "Atlantic 10", "MVC", "Sun Belt", "MAC",
            "Southern", "Patriot", "MEAC"]
CLASSES = ["Fr", "So", "Jr", "Sr"]
POSITIONS = ["Pure PG", "Combo G", "Wing G", "Wing F", "Stretch 4", "C"]

PLAYER_FIELDS = [
    "name", "team", "conference", "division", "class", "position", "season",
    "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg", "dreb_pg",
    "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg", "tov_pg",
    "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct", "ft_pct",
    "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg", "drtg",
    "bpm", "source", "updated_at",
]


def _rand_player(i: int, division: str, season: int, rng: random.Random, with_advanced: bool) -> dict:
    conf = rng.choice(D1_CONFS)
    pos = rng.choice(POSITIONS)
    big = pos in ("Stretch 4", "C", "Wing F")
    guard = pos in ("Pure PG", "Combo G", "Wing G")
    pts = round(rng.uniform(4, 24), 1)
    reb = round(rng.uniform(8, 12) if big else rng.uniform(2, 6), 1)
    oreb = round(reb * rng.uniform(0.25, 0.4), 1)
    rec = {
        "name": f"{division} Demo Player {i}",
        "team": f"{division} Demo U {i % 40}",
        "conference": conf,
        "division": division,
        "class": rng.choices(CLASSES, weights=[2, 2, 3, 4])[0],
        "position": pos,
        "season": season,
        "gp": rng.randint(18, 34),
        "min_pg": round(rng.uniform(12, 36), 1),
        "pts_pg": pts,
        "reb_pg": reb,
        "oreb_pg": oreb,
        "dreb_pg": round(reb - oreb, 1),
        "ast_pg": round(rng.uniform(2, 7) if guard else rng.uniform(0.3, 2.5), 1),
        "stl_pg": round(rng.uniform(0.3, 2.2), 1),
        "blk_pg": round(rng.uniform(0.8, 2.5) if big else rng.uniform(0.0, 0.6), 1),
        "tov_pg": round(rng.uniform(0.8, 3.5), 1),
        "fg_pct": round(rng.uniform(40, 58), 1),
        "fg2_pct": round(rng.uniform(45, 62), 1),
        "fg3_pct": round(rng.uniform(28, 42), 1),
        "ft_pct": round(rng.uniform(60, 90), 1),
        "source": "DEMO_SYNTHETIC",
    }
    if with_advanced:  # D1 gets advanced metrics; D2 leaves them NULL.
        rec.update({
            "min_pct": round(rec["min_pg"] / 40 * 100, 1),
            "orb_pct": round(rng.uniform(2, 14), 1),
            "drb_pct": round(rng.uniform(8, 28), 1),
            "ast_pct": round(rng.uniform(5, 35), 1),
            "to_pct": round(rng.uniform(8, 24), 1),
            "blk_pct": round(rng.uniform(0.2, 9), 1),
            "stl_pct": round(rng.uniform(0.5, 4), 1),
            "fg3a_rate": round(rng.uniform(10, 60), 1),
            "fta_rate": round(rng.uniform(15, 60), 1),
            "efg_pct": round(rng.uniform(45, 62), 1),
            "ts_pct": round(rng.uniform(48, 65), 1),
            "usage": round(rng.uniform(12, 32), 1),
            "ortg": round(rng.uniform(92, 124), 1),
            "drtg": round(rng.uniform(92, 112), 1),
            "bpm": round(rng.uniform(-4, 11), 1),
        })
    return rec


def seed(season: int, n_d1: int = 300, clear: bool = False, seed_val: int = 42) -> None:
    init_db()
    rng = random.Random(seed_val)
    if clear:
        with get_conn() as conn:
            conn.execute("DELETE FROM players WHERE source='DEMO_SYNTHETIC'")
            conn.commit()

    records = [_rand_player(i, "D1", season, rng, with_advanced=True) for i in range(n_d1)]

    now = utcnow_iso()
    placeholders = ",".join("?" for _ in PLAYER_FIELDS)
    update_cols = ",".join(f"{c}=excluded.{c}" for c in PLAYER_FIELDS
                           if c not in ("name", "team", "season", "division"))
    sql = (f"INSERT INTO players ({','.join(PLAYER_FIELDS)}) VALUES ({placeholders}) "
           f"ON CONFLICT(name, team, season, division) DO UPDATE SET {update_cols}")
    with get_conn() as conn:
        for rec in records:
            conn.execute(sql, [rec.get(f) if f != "updated_at" else now for f in PLAYER_FIELDS])
        conn.commit()

    # Conference strength: synthetic raw ratings for D1.
    d1_rows = [{"conference": c, "raw_rating": rng.uniform(0.3, 0.95)} for c in D1_CONFS]
    upsert_conferences(d1_rows, "D1", season, "DEMO_SYNTHETIC")

    print(f"[seed] inserted {len(records)} synthetic D1 players for season {season}")
    print("[seed] NOTE: source='DEMO_SYNTHETIC' — these are NOT real stats.")


def main():
    ap = argparse.ArgumentParser(description="Seed synthetic demo data")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--n-d1", type=int, default=300)
    args = ap.parse_args()
    seed(args.season, args.n_d1, clear=args.clear)


if __name__ == "__main__":
    main()
