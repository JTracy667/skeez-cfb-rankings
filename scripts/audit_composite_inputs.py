"""audit_composite_inputs.py — every input the composite weights must have REAL data.

A weighted composite converts "no data for this team" into the neutral default (50) with no
warning, so a broken data path is invisible: the board still renders, the numbers still look
plausible, and a third of the score is a constant. This asserts the opposite — it fails loudly
when any input the composite reads is missing for a team it ranks.

  usage: python scripts/audit_composite_inputs.py [--compare-old-merge]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")  # never start the scheduler

# Composite input -> the raw fields that must be present. Mirrors the reads in
# project_score_multi_factor(). Adding a read there means adding it here.
INPUTS = {
    "sp_norm":        [("sp_plus",)],
    "talent:247":     [("talent_score",)],
    "talent:recruit": [("recruiting_rank",)],
    "talent:return":  [("pct_ppa_returning",)],
    "eff:success":    [("off_success_rate", "def_success_rate")],
    "eff:epa":        [("epa_play", "def_epa_play")],
    "eff:ppo":        [("off_ppo", "def_ppo")],
    "eff:trench":     [("off_line_yards", "def_line_yards", "off_stuff_rate", "def_stuff_rate")],
    "eff:ppd":        [("pts_per_poss", "def_pts_per_poss")],
    "exp_norm":       [("experience_score",)],
}
DROP_9 = ["talent_score", "off_success_rate", "def_success_rate", "off_ppo", "def_ppo",
          "off_line_yards", "def_line_yards", "off_stuff_rate", "def_stuff_rate"]

# Gaps that are KNOWN and cannot be closed from the source: CFBD's /player/returning has no
# row for these FCS teams in any season we can reach. They impute the neutral default, which
# is the honest outcome. Anything NOT listed here is a defect — keep this list empty if you can.
KNOWN_GAPS = {"talent:return": {"North Dakota State", "Sacramento State"}}


def present(rec: dict, fields: tuple[str, ...]) -> bool:
    return all(rec.get(f) is not None for f in fields)


def coverage(m: dict) -> tuple[list[dict], dict[str, list[str]]]:
    """Universe = teams the board actually ranks (real ratings, not padding zeros)."""
    uni = [r for r in m.values() if r.get("sp_plus") or r.get("elo")]
    gaps: dict[str, list[str]] = {}
    for label, groups in INPUTS.items():
        miss = [r.get("name") for r in uni if not any(present(r, g) for g in groups)]
        if miss:
            gaps[label] = miss
    return uni, gaps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare-old-merge", action="store_true",
                    help="simulate the old whitelist by stripping the 9 dropped fields")
    a = ap.parse_args()

    import app  # noqa: E402 — after env setup so the scheduler stays off

    m = app._build_team_map()
    uni, gaps = coverage(m)
    print(f"board universe (teams with real ratings): {len(uni)}")
    print("\nCOVERAGE AFTER FIX:")
    for label, groups in INPUTS.items():
        have = len(uni) - len(gaps.get(label, []))
        print(f"  {label:<16} {have:>4}/{len(uni)}  {100.0 * have / len(uni):5.1f}%"
              + ("" if label not in gaps else f"   missing: {gaps[label][:6]}"))

    if a.compare_old_merge:
        old = {k: dict(v) for k, v in m.items()}
        for rec in old.values():
            for f in DROP_9:
                rec.pop(f, None)
        uni_o, gaps_o = coverage(old)
        print("\n--- OLD MERGE (whitelist) for comparison ---")
        for label, groups in INPUTS.items():
            have = len(uni_o) - len(gaps_o.get(label, []))
            print(f"  {label:<16} {have:>4}/{len(uni_o)}  {100.0 * have / len(uni_o):5.1f}%")
        print("\ntop 12 by composite, OLD merge -> NEW merge:")
        def order(mm):
            scored = sorted(((app.project_score_multi_factor(r, is_home=True).get("composite") or 0, r.get("name"))
                             for r in mm.values() if r.get("sp_plus") or r.get("elo")), reverse=True)
            return scored[-12:][::-1] if False else scored[:12], {n: i + 1 for i, (_, n) in enumerate(scored)}
        (o_top, o_rank), (n_top, n_rank) = order(old), order(m)
        for i in range(12):
            on, ov = o_top[i][1], o_top[i][0]
            nn = n_top[i][1]
            delta = (o_rank.get(nn, 0) - n_rank.get(nn, 0))
            print(f"   #{i+1:<3} old={on:<18}{ov:5.1f}   new={nn:<18}{n_top[i][0]:5.1f}   ({on} moved {delta:+d})")
    def unexplained() -> dict[str, list[str]]:
        return {k: [n for n in v if n not in KNOWN_GAPS.get(k, set())] for k, v in gaps.items()}

    bad = {k: v for k, v in unexplained().items() if v}
    known = {k: v for k, v in gaps.items() if k not in bad or v}
    if known and not bad:
        print("\nKNOWN, SOURCE-UNAVAILABLE (imputes neutral, not a defect):")
        for k, v in known.items():
            print(f"   {k}: {sorted(set(v))}")
    print("\nRESULT:", "PASS — every input has real data" if not bad else f"FAIL — gaps: {bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())