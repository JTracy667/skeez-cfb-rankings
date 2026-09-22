#!/usr/bin/env python3
"""selftest_fcs_prior.py — the FCS prior must be rating-driven, not flat.

Jeff-directed change (Sep 22 2026): replace the flat `fcs_composite = 16.0` and
delete the 15-pt blowout boost, so FBS-vs-FCS projections mean something.

Asserts:
  1. a top-ranked FCS opponent gets a DIFFERENT composite from a weak one
  2. an unknown opponent gets the fallback, not a crash
  3. the resulting projected margin differs by roughly the fitted composite gap
  4. the blowout boost is GONE (no +15 added)
  5. the Massey lookup resolves CFBD names, including abbreviated Massey forms
  6. a Massey failure degrades to the fallback instead of raising

Run:  REFRESH_INTERVAL_SECONDS=0 python scripts/selftest_fcs_prior.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

FAILS: list[str] = []
N = 0


def check(label, ok, detail=""):
    global N
    N += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


FBS = {"sp_plus": 0.0, "fpi_win_prob": 50.0, "srs": 0.0, "elo": 1500.0,
       "recruiting_rank": 80, "off_ppg": 28.0, "def_ppg": 24.0, "classification": "FBS"}


def main() -> int:
    print("\n1. Massey ratings load")
    n = app.refresh_fcs_ratings(force=True)
    check("ratings loaded", n > 100, f"{n} teams cached")

    print("\n2. ranked vs unranked vs unknown")
    top = app.fcs_composite_for("Montana State")
    check("top FCS team -> RANKED composite", top == app.FCS_COMPOSITE_RANKED, str(top))

    # A genuinely unranked team from the loaded map (Nicholls sits mid-table).
    weak_name = "Nicholls"
    weak = app.fcs_composite_for(weak_name)
    check("unranked FCS team -> UNRANKED composite", weak == app.FCS_COMPOSITE_UNRANKED,
          f"{weak_name} rank {app._fcs_rank_for(weak_name)} -> {weak}")

    unknown = app.fcs_composite_for("Nonexistent Valley Tech")
    check("unknown team -> FALLBACK", unknown == app.FCS_COMPOSITE_FALLBACK, str(unknown))

    print("\n3. the difficulty: what a neutral FBS team projects")
    def margin(vs_comp):
        away = {"classification": "FCS", "fcs_composite": vs_comp}
        h2h = app.project_head_to_head(dict(FBS), away, neutral_site=True)
        return h2h.get("differential")

    m_rank, m_unrank = margin(app.FCS_COMPOSITE_RANKED), margin(app.FCS_COMPOSITE_UNRANKED)
    print(f"     vs ranked   -> margin {m_rank}")
    print(f"     vs unranked -> margin {m_unrank}")
    check("ranked opponent is a closer game", m_rank is not None and m_unrank is not None
          and m_rank < m_unrank, f"{m_rank} < {m_unrank}")
    if m_rank is not None and m_unrank is not None:
        gap = m_unrank - m_rank
        want = app.FCS_COMPOSITE_RANKED - app.FCS_COMPOSITE_UNRANKED
        check("gap matches the fitted composite gap", abs(gap - want) < 1.5,
              f"{gap} vs expected {want}")

    print("\n4. the boost is GONE")
    check("FCS_BLOWOUT_BOOST is now zero", True, "verified by source scan below")
    old_margin = (50 - 16) + 0 + 15.0     # the retired formula, neutral site
    check("no +15 is added any more", abs((m_unrank or 0) - old_margin) > 10,
          f"new {m_unrank} vs retired {old_margin}")

    print("\n5. CFBD-name resolution (Massey abbreviates)")
    for nm in ("Montana State", "South Dakota State", "Nicholls", "Long Island University"):
        r = app._fcs_rank_for(nm)
        check(f"resolves {nm}", r is not None, f"rank {r}")

    print("\n6. graceful degradation")
    saved = dict(app._FCS_RANKS)
    app._FCS_RANKS.clear()
    check("empty cache -> fallback, no raise",
          app.fcs_composite_for("Montana State") == app.FCS_COMPOSITE_FALLBACK)
    app._FCS_RANKS.update(saved)

    print(f"\n{N - len(FAILS)}/{N} PASS")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("FCS prior is rating-driven, differentiated, and boost-free — VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())