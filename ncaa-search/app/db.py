"""SQLite connection helpers and schema management for the NCAA search engine.

Single-file DB at data/ncaa.db. We use plain sqlite3 (no ORM) to keep the
dependency surface small and the schema explicit.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Resolve data dir relative to the project root (ncaa-search/), regardless of CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "ncaa.db"


def db_path() -> Path:
    """Location of the SQLite file. Overridable via NCAA_DB env var (handy for tests)."""
    env = os.environ.get("NCAA_DB")
    return Path(env) if env else DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Stores RAW per-source values. Normalization to percentiles happens at query
# time (see app/scoring.py). NULLs are allowed everywhere except identity cols
# because D2 sources frequently lack advanced metrics.
SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    team        TEXT,
    conference  TEXT,
    division    TEXT NOT NULL,            -- 'D1' | 'D2'
    class       TEXT,                     -- 'Fr'|'So'|'Jr'|'Sr'|NULL
    position    TEXT,
    season      INTEGER NOT NULL,

    gp          REAL,
    min_pg      REAL,
    min_pct     REAL,

    pts_pg      REAL,
    reb_pg      REAL,
    oreb_pg     REAL,
    dreb_pg     REAL,
    orb_pct     REAL,
    drb_pct     REAL,

    ast_pg      REAL,
    ast_pct     REAL,
    stl_pg      REAL,
    blk_pg      REAL,
    tov_pg      REAL,
    to_pct      REAL,
    blk_pct     REAL,
    stl_pct     REAL,

    fg_pct      REAL,
    fg2_pct     REAL,
    fg3_pct     REAL,
    ft_pct      REAL,
    fg3a_rate   REAL,
    fta_rate    REAL,
    efg_pct     REAL,
    ts_pct      REAL,

    usage       REAL,
    ortg        REAL,
    drtg        REAL,
    bpm         REAL,

    source      TEXT,
    updated_at  TEXT,
    UNIQUE(name, team, season, division)
);

CREATE TABLE IF NOT EXISTS conferences (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conference      TEXT NOT NULL,
    division        TEXT NOT NULL,
    season          INTEGER NOT NULL,
    strength_rating REAL,                 -- normalized 0..1 within (division, season)
    rank            INTEGER,
    raw_rating      REAL,                 -- pre-normalization value from source
    source          TEXT,
    updated_at      TEXT,
    UNIQUE(conference, division, season)
);

CREATE INDEX IF NOT EXISTS idx_players_filter
    ON players(division, season, class);
CREATE INDEX IF NOT EXISTS idx_players_conf
    ON players(conference, division, season);
CREATE INDEX IF NOT EXISTS idx_conf_lookup
    ON conferences(division, season);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sensible pragmas and Row access."""
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(path: Path | str | None = None) -> None:
    """Create tables/indexes if they don't exist."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema at {db_path()}")
