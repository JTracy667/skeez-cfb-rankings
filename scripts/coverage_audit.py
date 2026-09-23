"""coverage_audit.py — does every input we weight actually RETURN data for the teams?

WHY THIS EXISTS: a weighted composite silently converts "we have no data for this team"
into the neutral default (50). If an input is missing for most teams, its fitted weight is
an artefact of a near-constant column, not a measured relationship — and nothing in the fit
output looks wrong. This audits coverage on the SAME point-in-time files the backtests used,
so we can tell whether a weight was fitted on real variation or on imputed noise.

  usage: python scripts/coverage_audit.py [--seasons 2021 2022 2023 2024 2025]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "backtest_cache"

# Input -> the raw fields that must be present for it to carry signal. A composite input is
# "real" for a team only if at least one of its source fields is present.
INPUTS = {
    "sp_plus (SP+ rating)":        ["sp_plus"],
    "fpi (FPI)":                   ["fpi", "fpi_win_prob"],
    "srs (SRS)":                   ["srs"],
    "elo (Elo)":                   ["elo", "elo_rating"],
    "talent (247 composite)":      ["talent_score"],
    "recruiting_rank":             ["recruiting_rank"],
    "returning production":        ["pct_ppa_returning"],
    "experience_score":            ["experience_score"],
    "efficiency: off_success":     ["off_success_rate"],
    "efficiency: epa":             ["epa_play", "off_epa"],
    "efficiency: ppo":             ["off_ppo"],
    "efficiency: line yards":      ["off_line_yards"],
    "efficiency: ppd":             ["off_ppg", "off_pts_per_poss"],
}


def rows_of(obj) -> list[dict]:
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for k in ("teams", "data", "records"):
            if isinstance(obj.get(k), list):
                return [r for r in obj[k] if isinstance(r, dict)]
        # {team_name: {...}}
        out = []
        for name, v in obj.items():
            if isinstance(v, dict):
                out.append({"team": name, **v})
        return out
    return []


def audit(seasons: list[int]) -> None:
    print(f"{'season':<8}{'teams':<8}" + "".join(f"{k.split(' ')[0]:<14}" for k in INPUTS))
    agg: dict[str, list[int]] = {k: [] for k in INPUTS}
    for s in seasons:
        fp = CACHE / f"preseason_{s}.json"
        if not fp.exists():
            print(f"{s:<8}MISSING FILE ({fp.name})")
            continue
        rows = rows_of(json.loads(fp.read_text(encoding="utf-8")))
        n = len(rows)
        line = f"{s:<8}{n:<8}"
        for k, fields in INPUTS.items():
            have = 0
            for r in rows:
                for f in fields:
                    v = r.get(f)
                    if v is not None and v != "" and v != 0:
                        have += 1
                        break
            pct = (100.0 * have / n) if n else 0.0
            agg[k].append(pct)
            line += f"{pct:>6.1f}%      "
        print(line)
    print()
    print("MEAN COVERAGE ACROSS SEASONS (the number that decides if a weight means anything):")
    for k, v in sorted(agg.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]) if kv[1] else 0)):
        if v:
            mean = sum(v) / len(v)
            flag = "  <-- NEAR-CONSTANT, weight is noise" if mean < 25 else ""
            print(f"   {k:<30} {mean:6.1f}%{flag}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    a = ap.parse_args()
    audit(a.seasons)