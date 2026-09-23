#!/usr/bin/env python3
"""selftest_composite_config.py — the weights refactor must not move any number.

Moving the composite weights out of the formula body and into COMPOSITE_CONFIG is
only safe if it is BEHAVIOUR-PRESERVING. This asserts that, plus the risk-register
D3 requirement that the active config is hashed into model_version so a weight
change creates a visible version break.

Checks:
  1. the enabled weights are exactly the documented originals
  2. they sum to 1.0 (the projected-score calibration assumes a 0-100 composite)
  3. fcs_rating and massey are INERT: adding an fcs_rating to a team does not
     change its composite while the weight is 0.0
  4. composite_version() is deterministic, and CHANGES when a weight changes
  5. a bad COMPOSITE_WEIGHTS_JSON is ignored rather than crashing

Run:  REFRESH_INTERVAL_SECONDS=0 python scripts/selftest_composite_config.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")   # no scheduler thread
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

FAILS: list[str] = []
N = 0

ORIGINAL = {"sp_plus": 0.18, "fpi": 0.15, "srs": 0.12, "elo": 0.08,
            "talent": 0.10, "efficiency": 0.37}


def check(label: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    cfg = app.composite_config()

    print("\n1. enabled weights are the documented originals")
    for k, v in ORIGINAL.items():
        check(f"{k} == {v}", abs(float(cfg.get(k, -1)) - v) < 1e-9, str(cfg.get(k)))

    print("\n2. weights sum to 1.0")
    total = sum(float(cfg[k]) for k in ORIGINAL)
    check("sum == 1.0", abs(total - 1.0) < 1e-9, f"{total:.6f}")

    print("\n3. fcs_rating / massey are INERT (weight 0.0 => no behaviour change)")
    check("fcs_rating weight is 0.0", float(cfg["fcs_rating"]) == 0.0, str(cfg["fcs_rating"]))
    check("massey weight is 0.0", float(cfg["massey"]) == 0.0, str(cfg["massey"]))

    sample = {"sp_plus": 12.0, "fpi": 0.7, "srs_score": 8.0, "elo": 1650.0,
              "rec_rank": 20, "talent_score": 700.0, "pct_ppa_returning": 60.0}
    without = app.project_score_multi_factor(dict(sample), is_home=True)["composite"]
    with_fcs = app.project_score_multi_factor({**sample, "fcs_rating": 99.0}, is_home=True)["composite"]
    check("composite unchanged by an FCS rating", abs(without - with_fcs) < 1e-9,
          f"{without} vs {with_fcs}")

    print("\n4. composite_version() is a real config hash (risk register D3)")
    v1 = app.composite_version()
    v2 = app.composite_version()
    check("deterministic within one config", v1 == v2, v1)
    check("looks like a hash", v1.startswith("c") and len(v1) == 11, v1)

    os.environ["COMPOSITE_WEIGHTS_JSON"] = json.dumps({"elo": 0.09, "srs": 0.11})
    try:
        v3 = app.composite_version()
        check("changes when a weight changes", v3 != v1, f"{v1} -> {v3}")
        check("live config reflects the override",
              abs(float(app.composite_config()["elo"]) - 0.09) < 1e-9,
              str(app.composite_config()["elo"]))
    finally:
        del os.environ["COMPOSITE_WEIGHTS_JSON"]
    check("reverts to the default config", app.composite_version() == v1)

    print("\n5. a malformed override is ignored, not fatal")
    os.environ["COMPOSITE_WEIGHTS_JSON"] = "{not json"
    try:
        cfg2 = app.composite_config()
        check("falls back to defaults", abs(float(cfg2["elo"]) - 0.08) < 1e-9, str(cfg2["elo"]))
    finally:
        del os.environ["COMPOSITE_WEIGHTS_JSON"]

    print(f"\n{N - len(FAILS)}/{N} PASS   model_version would be {v1}")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("composite config: single source of truth, behaviour-preserving, hashed — VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())