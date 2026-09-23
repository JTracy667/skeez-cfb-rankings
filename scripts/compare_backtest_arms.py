"""Compare two point-in-time backtest runs (baseline vs an alternative weighting).

Reads the summary + game-audit fixture from each arm and reports ATS/totals rates side by
side, plus two diagnostics that the summary alone does not give:

  * how often each arm takes the UNDERDOG (a model whose margins are too tight will take
    the dog far more than half the time, and lose);
  * mean |model spread| vs mean |book spread| — a direct measurement of margin
    COMPRESSION. If the model's margins are systematically smaller in magnitude than the
    market's, that is the mechanism behind an underdog bias, stated numerically rather
    than asserted.

Usage:
  python scripts/compare_backtest_arms.py A_summary A_fixture B_summary B_fixture \
      --labels baseline zero-srs
"""
from __future__ import annotations

import argparse
import json


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rate(w, l):
    d = w + l
    return (100.0 * w / d) if d else 0.0


def arm_stats(fixture):
    fbs = [g for g in fixture if g.get("is_fbs_matchup")]
    graded = [g for g in fbs if g.get("ats_result") in ("WIN", "LOSS")]
    w = sum(1 for g in graded if g["ats_result"] == "WIN")

    dogs = [g for g in graded
            if (g["closing_spread"] < 0 and g["ats_pick"].startswith(g["away"]))
            or (g["closing_spread"] > 0 and g["ats_pick"].startswith(g["home"]))]
    favs = [g for g in graded if g not in dogs]

    def sub(xs):
        ww = sum(1 for g in xs if g["ats_result"] == "WIN")
        return len(xs), ww, len(xs) - ww, rate(ww, len(xs) - ww)

    model_mag = sum(abs(g["model_proj_spread"]) for g in graded) / len(graded) if graded else 0
    book_mag = sum(abs(g["closing_spread"]) for g in graded) / len(graded) if graded else 0

    # Signed error relative to the book: positive = model favours home more than the book.
    err = [(g["model_proj_spread"] - (-g["closing_spread"])) for g in graded]
    mae = sum(abs(e) for e in err) / len(err) if err else 0
    bias = sum(err) / len(err) if err else 0

    return {
        "graded": len(graded),
        "ats_w": w, "ats_l": len(graded) - w, "ats_pct": rate(w, len(graded) - w),
        "dogs": sub(dogs), "favs": sub(favs),
        "model_mag": model_mag, "book_mag": book_mag,
        "mae": mae, "bias": bias,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a_summary"); ap.add_argument("a_fixture")
    ap.add_argument("b_summary"); ap.add_argument("b_fixture")
    ap.add_argument("--labels", nargs=2, default=["baseline", "alternative"])
    args = ap.parse_args()

    sa, sb = load(args.a_summary), load(args.b_summary)
    fa, fb = load(args.a_fixture), load(args.b_fixture)
    la, lb = args.labels

    print(f"  A = {la}    B = {lb}")
    print(f"  games evaluated: A={sa['total_evaluated_games']}  B={sb['total_evaluated_games']}"
          f"   FBS matchups: A={sa['fbs_matchups_evaluated']}  B={sb['fbs_matchups_evaluated']}")
    print()

    print("  SPREADS (ATS)")
    print(f"    {'tier':<22} {'A':>16} {'B':>16}   delta")
    for k in sa["fbs_ats_tiers"]:
        a, b = sa["fbs_ats_tiers"][k], sb["fbs_ats_tiers"][k]
        print(f"    {k:<22} {a['record']:>9} {a['win_pct']:>6.1f}% "
              f"{b['record']:>9} {b['win_pct']:>6.1f}%   {b['win_pct']-a['win_pct']:+.1f}")

    print("\n  TOTALS (O/U)")
    print(f"    {'tier':<22} {'A':>16} {'B':>16}   delta")
    for k in sa["fbs_totals_tiers"]:
        a, b = sa["fbs_totals_tiers"][k], sb["fbs_totals_tiers"][k]
        print(f"    {k:<22} {a['record']:>9} {a['win_pct']:>6.1f}% "
              f"{b['record']:>9} {b['win_pct']:>6.1f}%   {b['win_pct']-a['win_pct']:+.1f}")

    print("\n  WALK-FORWARD (spreads, all FBS)")
    for k in sa["walk_forward_weeks"]:
        a = sa["walk_forward_weeks"][k]["all_fbs_record"]
        b = sb["walk_forward_weeks"].get(k, {}).get("all_fbs_record", "n/a")
        print(f"    {k:<10} A: {a:<22} B: {b}")

    A, B = arm_stats(fa), arm_stats(fb)
    print("\n  DIAGNOSTICS")
    for name, st in ((la, A), (lb, B)):
        n, w, l, p = st["dogs"]
        fn, fw, fl, fp = st["favs"]
        print(f"    {name}:")
        print(f"      took the underdog : {w}-{l} = {p:.1f}%   ({100*n/st['graded']:.0f}% of picks)")
        print(f"      took the favourite: {fw}-{fl} = {fp:.1f}%")
        print(f"      mean |model margin| {st['model_mag']:.2f}  vs  mean |book line| {st['book_mag']:.2f}"
              f"   (compression {st['model_mag']-st['book_mag']:+.2f} pts)")
        print(f"      vs book: MAE {st['mae']:.2f} pts, bias {st['bias']:+.2f} pts")


if __name__ == "__main__":
    main()