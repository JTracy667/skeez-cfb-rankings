#!/usr/bin/env python3
"""
Compute SRS (Simple Rating System) from 2026 game results.

SRS solves: R_i - R_j + HFA ≈ margin_ij as a linear system over all completed games.
Output is normalized to [-25, +25] to match the app's srs_norm formula.

Usage:
    python scripts/compute_srs.py              # human-readable report
    python scripts/compute_srs.py --json        # JSON output for integration
    python scripts/compute_srs.py --min-games 2
"""

import json
import sys
import os
import math
from collections import defaultdict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MIN_GAMES_DEFAULT = 3
HFA = 2.5               # standard CFB home-field advantage (points)
MARGIN_CAP = 28.0       # cap blowout margins to prevent scale distortion
TARGET_RANGE = 25.0     # output SRS will be in [-TARGET_RANGE, +TARGET_RANGE]
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
GAMES_FILE = os.path.join(DATA_DIR, "cfbd_season_games.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "cfbd_analytics.json")

# Only compute SRS for FBS and FCS teams (the composite's universe).
# D2/D3/NAIA games are excluded — they distort the scale without adding signal.
VALID_CLASSIFICATIONS = {"fbs", "fcs"}


def load_completed_games(path: str = GAMES_FILE) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    games = data.get("games", data) if isinstance(data, dict) else data
    return [
        g for g in games
        if g.get("completed")
        and g.get("homePoints") is not None
        and g.get("awayPoints") is not None
        and g.get("homeTeam")
        and g.get("awayTeam")
    ]


def filter_fbs_fcs(games: list[dict]) -> list[dict]:
    """Keep only games where both teams are FBS or FCS."""
    result = []
    for g in games:
        hc = (g.get("homeClassification") or "").lower()
        ac = (g.get("awayClassification") or "").lower()
        if hc in VALID_CLASSIFICATIONS and ac in VALID_CLASSIFICATIONS:
            result.append(g)
    return result


def compute_srs(
    games: list[dict],
    hfa: float = HFA,
    margin_cap: float = MARGIN_CAP,
    max_iter: int = 500,
    tol: float = 0.0005,
    min_games: int = MIN_GAMES_DEFAULT,
) -> dict[str, dict]:
    """
    Compute SRS via the linear system R_i = MOV_i + avg(R_opponents).

    Margin capping prevents blowouts from distorting the scale.
    HFA is applied only to non-neutral games.

    Returns: {team_name: {"srs_raw": float, "games": int, "mov": float, "sos": float}}
    """
    # Build per-team records
    teams: dict[str, dict] = {}
    game_list = []  # (home, away, adjusted_margin)

    for g in games:
        h, a = g["homeTeam"], g["awayTeam"]
        hp, ap = g["homePoints"], g["awayPoints"]
        neutral = g.get("neutralSite", False)

        margin = float(hp - ap)
        # Cap margin
        margin = max(-margin_cap, min(margin_cap, margin))
        # Adjust for HFA: rating_diff + HFA ≈ margin → true_diff = margin - HFA
        if not neutral:
            adj_margin = margin - hfa
        else:
            adj_margin = margin

        game_list.append((h, a, adj_margin))

        for t in (h, a):
            if t not in teams:
                teams[t] = {"games": 0, "margins": [], "opponents": []}

        teams[h]["games"] += 1
        teams[h]["margins"].append(adj_margin)
        teams[h]["opponents"].append(a)
        teams[a]["games"] += 1
        teams[a]["margins"].append(-adj_margin)
        teams[a]["opponents"].append(h)

    # Iterative solver: R_i = MOV_i + avg(R_opponents)
    ratings = {t: 0.0 for t in teams}

    for iteration in range(max_iter):
        new_ratings = {}
        for t, info in teams.items():
            mov = sum(info["margins"]) / len(info["margins"])
            sos = sum(ratings[o] for o in info["opponents"]) / len(info["opponents"])
            new_ratings[t] = mov + sos
        # Normalize: mean = 0
        mean_r = sum(new_ratings.values()) / len(new_ratings)
        new_ratings = {t: r - mean_r for t, r in new_ratings.items()}
        delta = max(abs(new_ratings[t] - ratings[t]) for t in ratings)
        ratings = new_ratings
        if delta < tol:
            break

    # Build result with metadata
    result = {}
    for t, info in teams.items():
        mov = sum(info["margins"]) / len(info["margins"])
        sos = sum(ratings[o] for o in info["opponents"]) / len(info["opponents"])
        result[t] = {
            "srs_raw": ratings[t],
            "games": info["games"],
            "mov": round(mov, 2),
            "sos": round(sos, 2),
            "min_games_met": info["games"] >= min_games,
        }

    return result


def normalize_to_target_range(
    raw: dict[str, dict], target: float = TARGET_RANGE
) -> dict[str, dict]:
    """Rescale raw SRS values to [-target, +target] using 5th/95th percentiles."""
    values = sorted(v["srs_raw"] for v in raw.values())
    n = len(values)
    if n < 10:
        return raw

    # Use 2nd and 98th percentiles as anchors (robust to outliers)
    p_lo = values[int(n * 0.02)]
    p_hi = values[int(n * 0.98)]
    mid = (p_hi + p_lo) / 2
    half_range = (p_hi - p_lo) / 2

    if half_range < 0.01:
        return raw

    scale = target / half_range

    result = {}
    for t, v in raw.items():
        v2 = dict(v)
        v2["srs"] = round(max(-target, min(target, (v["srs_raw"] - mid) * scale)), 2)
        result[t] = v2
    return result


def load_cfbd_2025_srs(path: str = ANALYTICS_FILE) -> dict[str, float]:
    """Load CFBD's 2025 SRS values — only teams with real (non-zero) ratings."""
    with open(path) as f:
        data = json.load(f)
    result = {}
    for team in data:
        name = team.get("name", "")
        srs = team.get("srs")
        # Only include teams where SRS is a real value (not 0.0 placeholder)
        # CFBD SRS ranges roughly [-45, +35]; 0.0 means "no data"
        if name and srs is not None and abs(srs) > 0.5:
            result[name] = srs
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Compute SRS from 2026 game results")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-games", type=int, default=MIN_GAMES_DEFAULT)
    args = parser.parse_args()

    min_games = args.min_games

    # Load and filter games
    all_games = load_completed_games()
    games = filter_fbs_fcs(all_games)

    print(f"SRS Computation — 2026 Season")
    print(f"=" * 60)
    print(f"All completed games: {len(all_games)}")
    print(f"FBS+FCS games used: {len(games)}")
    print(f"HFA: {HFA} pts | Margin cap: ±{MARGIN_CAP} pts")

    # Compute
    srs_raw = compute_srs(games, min_games=min_games)
    srs = normalize_to_target_range(srs_raw)

    qualified = {t: v for t, v in srs.items() if v["min_games_met"]}
    unqualified = {t: v for t, v in srs.items() if not v["min_games_met"]}

    print(f"Teams rated: {len(srs)} (qualified: {len(qualified)}, provisional: {len(unqualified)})")

    if args.json:
        output = {
            "hfa": HFA,
            "margin_cap": MARGIN_CAP,
            "min_games": args.min_games,
            "total_games": len(games),
            "ratings": {t: v["srs"] for t, v in srs.items()},
            "detail": srs,
        }
        print(json.dumps(output, indent=2))
        return

    # Top 25
    top = sorted(qualified.items(), key=lambda x: -x[1]["srs"])[:25]
    print(f"\nTop 25 SRS (2026 computed, >= {min_games} games):")
    print(f"  {'#':>3}  {'Team':<30} {'SRS':>7} {'G':>3} {'MOV':>7} {'SOS':>7}")
    print(f"  {'---'}  {'-'*30} {'---'} {'---'} {'---'} {'---'}")
    for i, (team, v) in enumerate(top, 1):
        print(f"  {i:>3}  {team:<30} {v['srs']:>7.2f} {v['games']:>3} {v['mov']:>7.2f} {v['sos']:>7.2f}")

    # Bottom 10 qualified
    bottom = sorted(qualified.items(), key=lambda x: x[1]["srs"])[:10]
    print(f"\nBottom 10 (qualified):")
    for team, v in bottom:
        print(f"  {team:<30} {v['srs']:>7.2f} {v['games']:>3} {v['mov']:>7.2f} {v['sos']:>7.2f}")

    # FCS teams specifically
    fcs_teams = []
    for g in games:
        for t, c in [(g["homeTeam"], g.get("homeClassification","")), (g["awayTeam"], g.get("awayClassification",""))]:
            if c == "fcs" and t in srs:
                fcs_teams.append(t)
    fcs_unique = list(set(fcs_teams))
    fcs_rated = [(t, srs[t]) for t in fcs_unique if srs[t]["min_games_met"]]
    fcs_rated.sort(key=lambda x: -x[1]["srs"])
    print(f"\nFCS teams rated: {len(fcs_rated)}")
    if fcs_rated:
        print(f"  {'Team':<30} {'SRS':>7} {'G':>3}")
        for t, v in fcs_rated[:10]:
            print(f"  {t:<30} {v['srs']:>7.2f} {v['games']:>3}")
        if len(fcs_rated) > 10:
            print(f"  ... and {len(fcs_rated)-10} more")

    # Compare to CFBD 2025
    try:
        cfbd_2025 = load_cfbd_2025_srs()
        # Only compare teams that have BOTH our rating and CFBD 2025 rating
        common = [(t, srs[t]["srs"], cfbd_2025[t])
                  for t in srs
                  if t in cfbd_2025 and srs[t]["min_games_met"]]
        if common:
            n = len(common)
            our_vals = [c[1] for c in common]
            their_vals = [c[2] for c in common]
            mean_our = sum(our_vals) / n
            mean_their = sum(their_vals) / n
            cov = sum((our_vals[i]-mean_our)*(their_vals[i]-mean_their) for i in range(n)) / n
            std_our = (sum((v-mean_our)**2 for v in our_vals)/n)**0.5
            std_their = (sum((v-mean_their)**2 for v in their_vals)/n)**0.5
            corr = cov/(std_our*std_their) if std_our*std_their > 0 else 0

            diffs = sorted(common, key=lambda x: -abs(x[1]-x[2]))
            print(f"\nComparison to CFBD 2025 SRS (real ratings only):")
            print(f"  Teams compared: {n}")
            print(f"  Correlation: {corr:.4f}")
            print(f"  Mean |diff|: {sum(abs(c[1]-c[2]) for c in common)/n:.2f}")
            print(f"\n  Largest divergences:")
            print(f"  {'Team':<30} {'Ours(26)':>9} {'CFBD(25)':>9} {'Diff':>7}")
            print(f"  {'-'*30} {'-'*9} {'-'*9} {'-'*7}")
            for t, ours, theirs in diffs[:15]:
                print(f"  {t:<30} {ours:>9.2f} {theirs:>9.2f} {ours-theirs:>+7.2f}")
        else:
            print("\n  No common teams for comparison")
    except FileNotFoundError:
        print("\n  (No CFBD analytics file for comparison)")

    if unqualified:
        print(f"\n  Provisional ratings (< {min_games} games) available with --min-games 1")


if __name__ == "__main__":
    main()
