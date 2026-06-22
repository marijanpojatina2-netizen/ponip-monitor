"""Build/refresh the `conferences` table.

D1 strength comes from Torvik team ratings aggregated per conference (see
torvik_d1.build_conferences which calls into here). D2 strength comes from the
editable CSV of manual tiers (data/d2_conference_tiers.csv).

In all cases strength_rating is min-max normalized to 0..1 WITHIN (division,
season) so D1 and D2 are each self-contained scales.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.db import get_conn, init_db
from ingest.common import minmax_normalize, to_float, utcnow_iso

PROJECT_ROOT = Path(__file__).resolve().parent.parent
D2_TIERS_CSV = PROJECT_ROOT / "data" / "d2_conference_tiers.csv"


def upsert_conferences(rows: list[dict], division: str, season: int, source: str) -> int:
    """rows: [{conference, raw_rating, rank?}]. Normalizes raw_rating -> 0..1 and writes."""
    if not rows:
        return 0
    raws = [to_float(r.get("raw_rating")) for r in rows]
    norm = minmax_normalize(raws)
    # Rank by raw_rating desc if not provided.
    order = sorted(range(len(rows)), key=lambda i: (raws[i] if raws[i] is not None else -1e9), reverse=True)
    rank_by_idx = {idx: pos + 1 for pos, idx in enumerate(order)}

    now = utcnow_iso()
    written = 0
    with get_conn() as conn:
        for i, r in enumerate(rows):
            conf = (r.get("conference") or "").strip()
            if not conf:
                continue
            rank = r.get("rank") or rank_by_idx.get(i)
            conn.execute(
                """
                INSERT INTO conferences
                    (conference, division, season, strength_rating, rank, raw_rating, source, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(conference, division, season) DO UPDATE SET
                    strength_rating=excluded.strength_rating,
                    rank=excluded.rank,
                    raw_rating=excluded.raw_rating,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (conf, division, season, norm[i], rank, raws[i], source, now),
            )
            written += 1
        conn.commit()
    return written


def load_d2_tiers(csv_path: Path = D2_TIERS_CSV) -> list[dict]:
    """Parse the manual D2 tier CSV (ignoring # comment lines)."""
    if not csv_path.exists():
        raise FileNotFoundError(f"D2 tiers CSV not found: {csv_path}")
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    for row in reader:
        conf = (row.get("conference") or "").strip()
        if not conf:
            continue
        tier = to_float(row.get("tier"), default=3.0)
        rows.append({"conference": conf, "raw_rating": tier})
    return rows


def build_d2_conferences(season: int) -> int:
    init_db()
    rows = load_d2_tiers()
    n = upsert_conferences(rows, division="D2", season=season, source="manual_tiers_csv")
    print(f"[conferences] D2 season {season}: wrote {n} conferences from {D2_TIERS_CSV.name}")
    return n


def main():
    ap = argparse.ArgumentParser(description="Build conferences table")
    ap.add_argument("--division", choices=["D1", "D2"], required=True)
    ap.add_argument("--season", type=int, required=True)
    args = ap.parse_args()
    if args.division == "D2":
        build_d2_conferences(args.season)
    else:
        # D1 conference strength is derived during D1 player ingestion.
        from ingest.torvik_d1 import build_conferences as build_d1
        build_d1(args.season)


if __name__ == "__main__":
    main()
