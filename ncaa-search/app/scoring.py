"""Percentile normalization + weighted composite scoring.

Design
------
* Each numeric metric is converted to a PERCENTILE (0..100) computed WITHIN a
  (division, season) population. Percentiles are stable regardless of the user's
  row filters (we percentile against the full division population, not the
  filtered subset) so a player's standing reflects all D1/D2 peers.
* The composite score is a weighted average of the player's percentile on each
  metric the user gave a positive weight to.
* "Lower is better" metrics (turnovers, TO%, DRtg) are inverted: 100 - pct.
* Conference strength is just another weightable metric. Its underlying value is
  already normalized 0..1 within division, so we pre-score it as value*100
  instead of re-percentiling (flag: prescored).
* NULL handling (configurable): default "exclude" drops a metric from THAT
  player's weighted average and renormalizes the remaining weights. "median"
  treats a missing metric as the 50th percentile.
* Cross-division: D2 composites are scaled by division_factor (default 0.85) to
  acknowledge the weaker level of competition; D1 = 1.0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    higher_is_better: bool = True
    prescored: bool = False  # already on a 0..100 scale; skip percentiling


# The full set of rankable metrics. `key` matches a column on players (except
# conf_strength which the API attaches from the conferences table).
METRICS: list[Metric] = [
    Metric("pts_pg", "Points / game"),
    Metric("reb_pg", "Rebounds / game"),
    Metric("oreb_pg", "Off. rebounds / game"),
    Metric("dreb_pg", "Def. rebounds / game"),
    Metric("orb_pct", "Offensive rebound %"),
    Metric("drb_pct", "Defensive rebound %"),
    Metric("ast_pg", "Assists / game"),
    Metric("ast_pct", "Assist %"),
    Metric("stl_pg", "Steals / game"),
    Metric("blk_pg", "Blocks / game"),
    Metric("stl_pct", "Steal %"),
    Metric("blk_pct", "Block %"),
    Metric("tov_pg", "Turnovers / game", higher_is_better=False),
    Metric("to_pct", "Turnover %", higher_is_better=False),
    Metric("fg_pct", "FG%"),
    Metric("fg2_pct", "2P%"),
    Metric("fg3_pct", "3P%"),
    Metric("ft_pct", "FT%"),
    Metric("fg3a_rate", "3PA rate"),
    Metric("fta_rate", "FT rate"),
    Metric("efg_pct", "eFG%"),
    Metric("ts_pct", "TS%"),
    Metric("usage", "Usage %"),
    Metric("ortg", "Offensive rating"),
    Metric("drtg", "Defensive rating", higher_is_better=False),
    Metric("bpm", "BPM / box +/-"),
    Metric("conf_strength", "Conference strength", prescored=True),
]

METRIC_BY_KEY = {m.key: m for m in METRICS}


def metric_registry() -> list[dict]:
    return [
        {"key": m.key, "label": m.label, "higher_is_better": m.higher_is_better}
        for m in METRICS
    ]


def _percentiles(values: list[Optional[float]]) -> list[Optional[float]]:
    """Midrank percentile (0..100) for non-null values; None stays None.

    pct(x) = 100 * (#{v < x} + 0.5 * #{v == x}) / N
    """
    present = [v for v in values if v is not None]
    n = len(present)
    if n == 0:
        return [None for _ in values]
    if n == 1:
        return [50.0 if v is not None else None for v in values]
    s = sorted(present)
    # For ties, precompute counts via binary search.
    import bisect
    out: list[Optional[float]] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        lo = bisect.bisect_left(s, v)
        hi = bisect.bisect_right(s, v)
        less = lo
        equal = hi - lo
        out.append(100.0 * (less + 0.5 * equal) / n)
    return out


def compute_percentile_table(players: Iterable[dict]) -> dict:
    """Return {player_id: {metric_key: percentile_or_None}} computed per
    (division, season) group. Inversion for lower-is-better metrics is applied
    here so downstream code always sees "higher percentile = better".

    `players` rows must contain `id`, `division`, `season`, and metric columns.
    For conf_strength the row should carry a `conf_strength` value in 0..100
    (prescored) — see API which derives it from strength_rating*100.
    """
    players = list(players)
    groups: dict[tuple, list[dict]] = {}
    for p in players:
        groups.setdefault((p["division"], p["season"]), []).append(p)

    result: dict = {}
    for _, group in groups.items():
        ids = [p["id"] for p in group]
        for m in METRICS:
            vals = [_safe_num(p.get(m.key)) for p in group]
            if m.prescored:
                pcts = [v for v in vals]  # already 0..100
            else:
                pcts = _percentiles(vals)
                if not m.higher_is_better:
                    pcts = [None if v is None else 100.0 - v for v in pcts]
            for pid, pct in zip(ids, pcts):
                result.setdefault(pid, {})[m.key] = pct
    return result


def _safe_num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def composite_score(
    pct_row: dict,
    weights: dict[str, float],
    *,
    division: str,
    null_policy: str = "exclude",
    division_factor: Optional[dict[str, float]] = None,
) -> Optional[float]:
    """Weighted average of percentiles for metrics with weight>0.

    Returns a 0..100-ish score scaled by division_factor, or None if no metric
    contributed.
    """
    division_factor = division_factor or {"D1": 1.0, "D2": 0.85}
    num = 0.0
    den = 0.0
    for key, w in weights.items():
        if not w or w <= 0:
            continue
        if key not in METRIC_BY_KEY:
            continue
        pct = pct_row.get(key)
        if pct is None:
            if null_policy == "median":
                pct = 50.0
            else:  # exclude
                continue
        num += w * pct
        den += w
    if den == 0:
        return None
    score = num / den
    score *= division_factor.get(division, 1.0)
    return round(score, 2)


# Sensible starter preset (used as the default landing weights): balanced
# production + efficiency + conference strength.
DEFAULT_WEIGHTS = {
    "pts_pg": 60,
    "reb_pg": 30,
    "ast_pg": 30,
    "ts_pct": 40,
    "efg_pct": 20,
    "stl_pg": 15,
    "blk_pg": 15,
    "to_pct": 20,        # inverted internally
    "ortg": 25,
    "conf_strength": 50,
}
