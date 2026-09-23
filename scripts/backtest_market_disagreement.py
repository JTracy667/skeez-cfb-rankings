"""backtest_market_disagreement.py — where OUR ranking disagrees with the MARKET.

MOTIVATION (Jeff, Sep 23 2026): "if we end up having good signal on conferences or specific
games that's fair. Like Florida being a favorite betting line over Ole Miss and Ole Miss is
ranked 4."

A market/model disagreement is the only place an edge CAN live: if the model and the market
always agree there is nothing to bet, and if the model is only right when the market agrees
it has no independent information. This study isolates the disagreement subset and asks
whether OUR side of it wins — straight up (does it grade teams better) and against the
number — overall and broken out by conference.

Model margin per row is reproduced with the V6.1 constants and the app's own talent blend,
so this reads the same composite the live board serves (decay OFF: we are measuring the
RANKING, which is a season-long grade, not a week-specific projection).

CAVEAT: conferences come from the CURRENT FBS membership file, so 2021-23 games are
classified by 2026 alignment (realignment). Read the per-conference lines as "programs that
are in this conference now", not as historical conference records.

  usage: python scripts/backtest_market_disagreement.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import backtest_talent_arms as ta  # noqa: E402
import app  # noqa: E402

POWER = {"SEC", "Big Ten", "Big 12", "ACC", "Pac-12"}


def model_margin(r: dict) -> float:
    th, tao = ta.talent_blend(r["hp"]), ta.talent_blend(r["ap"])
    sh, eh, xh = r["h_see"]
    sa, ea, xa = r["a_see"]
    W = ta.W
    ch = sh * W["sp_plus"] + eh * W["efficiency"] + xh * W["experience"] + th * W["talent"]
    ca = sa * W["sp_plus"] + ea * W["efficiency"] + xa * W["experience"] + tao * W["talent"]
    gap = ch - ca
    m = (1 if gap >= 0 else -1) * abs(gap) ** ta.POWER * ta.SLOPE
    return m + (0.0 if r["neutral"] else ta.HFA)


def pct(a: int, b: int) -> float:
    return (100.0 * a / b) if b else 0.0


def main() -> int:
    conf = app.load_fbs_conferences()
    rows = []
    for s in (2021, 2022, 2023, 2024, 2025):
        rows += ta.load_season(s)
    print(f"games loaded: {len(rows)}")

    agree, dis = [], []
    for r in rows:
        m = model_margin(r)
        book = r["book_home_margin"]
        act = r["actual_home_margin"]
        if m == 0 or book == 0:
            continue
        mf = "home" if m > 0 else "away"
        kf = "home" if book > 0 else "away"
        sign = 1.0 if mf == "home" else -1.0          # model's side is positive
        rec = {
            "season": r["season"], "week": r["week"],
            "pick": r["home"] if mf == "home" else r["away"],
            "opp": r["away"] if mf == "home" else r["home"],
            "pick_conf": conf.get(r["home"] if mf == "home" else r["away"], "?"),
            "opp_conf": conf.get(r["away"] if mf == "home" else r["home"], "?"),
            "su_win": (act * sign) > 0,
            "cover": ((act - book) * sign) > 0,
            "limit": (act - book) * sign,             # + = model side covered
            "model_edge": abs(m - book),              # how far from the number we sit
        }
        (dis if mf != kf else agree).append(rec)

    print(f"model disagrees with the market on {len(dis)} of {len(rows)} graded games "
          f"({pct(len(dis), len(rows)):.1f}%)")
    for label, group in (("ALL games (baseline)", agree + dis), ("markets AGREE with us", agree),
                         ("MARKET DISAGREES", dis)):
        su = sum(1 for x in group if x["su_win"])
        cv = sum(1 for x in group if x["cover"])
        n_ = len(group)
        print(f"  {label:<24} n={n_:<5} straight-up {pct(su, n_):5.1f}%   "
              f"ATS {pct(cv, n_):5.1f}%")

    print("\n  DISAGREEMENT by conference of OUR pick:")
    print(f"  {'conference':<14}{'n':>6}{'straight-up':>13}{'ATS':>8}{'edge':>8}")
    by_conf: dict[str, list] = {}
    for x in dis:
        by_conf.setdefault(x["pick_conf"], []).append(x)
    for c, g in sorted(by_conf.items(), key=lambda kv: -len(kv[1])):
        su = sum(1 for x in g if x["su_win"])
        cv = sum(1 for x in g if x["cover"])
        avg = sum(x["model_edge"] for x in g) / len(g)
        star = " *" if c in POWER else "  "
        print(f"  {c:<14}{len(g):>6}{pct(su, len(g)):>12.1f}%{pct(cv, len(g)):>7.1f}%{avg:>8.1f}{star}")

    print("\n  DISAGREEMENT by size of our edge off the number:")
    for lo, hi in ((0, 3), (3, 7), (7, 14), (14, 100)):
        g = [x for x in dis if lo <= x["model_edge"] < hi]
        if not g:
            continue
        su = sum(1 for x in g if x["su_win"])
        cv = sum(1 for x in g if x["cover"])
        print(f"  edge {lo:>2}-{hi:<3} n={len(g):<5} straight-up {pct(su, len(g)):5.1f}%   "
              f"ATS {pct(cv, len(g)):5.1f}%")

    print("\n  teams we most often price AGAINST the market (>= 8 disagreements):")
    by_team: dict[str, list] = {}
    for x in dis:
        by_team.setdefault(x["pick"], []).append(x)
    for t, g in sorted(by_team.items(), key=lambda kv: -len(kv[1]))[:12]:
        if len(g) < 8:
            break
        su = sum(1 for x in g if x["su_win"])
        cv = sum(1 for x in g if x["cover"])
        print(f"  {t:<24} n={len(g):<4} straight-up {pct(su, len(g)):5.1f}%   ATS {pct(cv, len(g)):5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())