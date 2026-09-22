#!/usr/bin/env python3
"""propose_fcs_baseline.py — the CONCRETE FCS proposal, in model output terms.

Answers: if the flat 16.0 goes away, what does the FCS side become?

Method: use the app's OWN projection functions (no reimplementation, so the
numbers are what the site would actually print). Sweep the FCS composite, compute
the projected margin vs a neutral (league-average, composite 50) FBS opponent, and
find the composite that reproduces the margins measured from 848 real
FBS-vs-FCS games:

    vs unranked FCS  -> FBS wins by 31.9
    vs ranked FCS    -> FBS wins by 14.2

Then show what the CURRENT 16.0 actually produces, so the size of the error is
explicit.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

# A league-average FBS team: every input at its neutral value (composite ~50).
FBS_NEUTRAL = {
    "sp_plus": 0.0, "fpi_win_prob": 50.0, "srs": 0.0, "elo": 1500.0,
    "recruiting_rank": 80, "talent_score": 700.0, "off_ppg": 28.0, "def_ppg": 24.0,
    "classification": "FBS",
}
FCS = {"classification": "FCS"}   # no ratings at all -> takes the FCS branch


def fcs_branch_score(comp: float, is_home: bool) -> float:
    """Exactly the FCS missing-data branch, parameterised by the composite."""
    base = max(6.0, 27.0 + (comp - 50.0) * 0.55)
    adj = 2.5 if is_home else -1.5
    return round(max(0.0, base + adj), 1)


def main() -> int:
    fb = app.project_score_multi_factor(dict(FBS_NEUTRAL), is_home=True)
    print(f"neutral FBS team: composite={fb['composite']:.1f} projected_score={fb['projected_score']}")
    print(f"  -> on a neutral field an average FBS team scores ~{fb['projected_score']:.1f}\n")

    print("FCS composite -> projected FBS margin (FBS home, neutral FBS opponent)")
    print(f"  {'FCS comp':>9} {'FCS pts':>8} {'FBS pts':>8} {'margin':>7}")
    rows = []
    for comp in (16, 20, 24, 28, 32, 36, 40, 44):
        fcs = fcs_branch_score(comp, is_home=False)
        fbs = app.project_score_multi_factor(dict(FBS_NEUTRAL), is_home=True,
                                             opp_composite=comp)["projected_score"]
        rows.append((comp, fcs, fbs, fbs - fcs))
        print(f"  {comp:>9} {fcs:>8.1f} {fbs:>8.1f} {fbs - fcs:>7.1f}")

    cur = [r for r in rows if r[0] == 16][0]
    print(f"\nCURRENT (flat 16.0): FBS wins by {cur[3]:.1f}")
    print(f"  measured vs UNRANKED FCS  : 31.9   -> error {cur[3] - 31.9:+.1f}")
    print(f"  measured vs RANKED FCS    : 14.2   -> error {cur[3] - 14.2:+.1f}")

    print("\nImplied composite for each measured margin (linear interp on the sweep):")
    for label, target in (("unranked FCS", 31.9), ("ranked FCS (1-25)", 14.2),
                          ("FCS rank ~1-3", -4.0), ("FCS rank ~10", 22.2),
                          ("FCS rank ~25", 23.6)):
        best = None
        for r in rows:
            d = abs(r[3] - target)
            if best is None or d < best[0]:
                best = (d, r)
        print(f"  {label:<20} margin {target:>6.1f}  -> FCS composite ~{best[1][0]}")

    print("\nNOTE: 'FCS comp ~1-3' implies a NEGATIVE margin (FBS loses), so the top")
    print("FCS teams need a composite ABOVE the FBS average's counterpart — i.e. the")
    print("curve must extend past the sweep above, not just below 16.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())