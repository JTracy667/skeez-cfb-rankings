#!/usr/bin/env python3
"""preflight_v61.py — before/after projections for the V6.1 deploy.

Picks three REAL upcoming games (a heavyweight mismatch, a ranked-vs-ranked, and an
FBS-vs-FCS) straight from the app's own schedule source (CFBD /games, cached to
data/backtest_cache/preflight_games.json so the before/after runs use identical inputs),
then prints the live projection for each through the app's own code path.

    python scripts/preflight_v61.py --tag before
    python scripts/preflight_v61.py --tag after
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")   # never start the live scheduler

CACHE = ROOT / "data" / "backtest_cache" / "preflight_games.json"
OUT = ROOT / "data" / "backtest_study"


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CFBD_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("CFBD_API_KEY not found")


def games(season: int, weeks: list[int]) -> list[dict]:
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if {r["week"] for r in cached} >= set(weeks):
            return cached
    rows = []
    for week in weeks:
        url = f"https://api.collegefootballdata.com/games?year={season}&week={week}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key()}",
                                                  "User-Agent": "cfb-analytics/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        rows += [{"id": g["id"], "home": g["homeTeam"], "away": g["awayTeam"],
                  "home_conf": g.get("homeConference"), "away_conf": g.get("awayConference"),
                  "neutral": g.get("neutralSite"), "week": week, "season": season}
                 for g in data if g.get("homeTeam") and g.get("awayTeam")]
    CACHE.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def pick(rows: list[dict], board: dict) -> list[dict]:
    """one big mismatch, one near-pick'em, one FBS-vs-FCS"""
    import app  # noqa: PLC0415

    def feats(n):
        td = board.get(n) or {}
        p = app.project_score_multi_factor(td)
        return (p.get("composite") or 0.0), p.get("data_flag")

    comp, is_fbs = {}, {}
    for n in {r["home"] for r in rows} | {r["away"] for r in rows}:
        c, fl = feats(n)
        comp[n], is_fbs[n] = c, fl != "fcs_no_data"

    fbs = [g for g in rows if is_fbs.get(g["home"]) and is_fbs.get(g["away"])]
    fbs.sort(key=lambda g: -abs(comp[g["home"]] - comp[g["away"]]))
    cross = [g for g in rows if is_fbs.get(g["home"]) != is_fbs.get(g["away"])]
    picks = []
    if fbs:
        picks.append(fbs[0])                                        # biggest mismatch
        near = sorted(fbs, key=lambda g: abs(abs(comp[g["home"]] - comp[g["away"]]) - 3))
        for g in near:
            if g not in picks:
                picks.append(g)                                     # near-pick'em
                break
    cross.sort(key=lambda g: -abs(comp[g["home"]] - comp[g["away"]]))
    if cross:
        picks.append(cross[0])                                      # FBS vs FCS
    return picks[:3]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--weeks", type=int, nargs="+", default=[2, 3, 4])
    ap.add_argument("--tag", default="run")
    ap.add_argument("--picks-from", default=None,
                    help="reuse the sample games from a previous preflight JSON, so a "
                         "before/after comparison is like-for-like (the picker sorts by "
                         "composite, and the composite CHANGES between runs)")
    args = ap.parse_args()

    import app  # noqa: E402  (after REFRESH_INTERVAL_SECONDS=0)

    board = app._build_team_map() or {}
    rows = games(args.season, args.weeks)
    if args.picks_from:
        prev = json.loads(Path(args.picks_from).read_text(encoding="utf-8"))
        wanted = [(g["home"], g["away"]) for g in prev["games"]]
        picks = [g for pair in wanted for g in rows if (g["home"], g["away"]) == pair]
        print(f"[preflight] reusing {len(picks)} sample games from {args.picks_from}")
    else:
        picks = pick(rows, board)
    print(f"[preflight] composite config {json.dumps(app.composite_config())}")
    print(f"[preflight] model_version {app.composite_version()}  "
          f"enabled_sum {sum(app.composite_config().get(k,0) for k in app._ENABLED_KEYS):.4f}")
    print(f"[preflight] {len(rows)} games available across weeks {args.weeks}; "
          f"{len(picks)} sampled\n")
    out = {"tag": args.tag, "config": app.composite_config(),
           "model_version": app.composite_version(), "games": []}
    for g in picks:
        hd, ad = board.get(g["home"]), board.get(g["away"])
        if not hd or not ad:
            print(f"  SKIP {g['away']} @ {g['home']} — not in team map")
            continue
        h2h = app.project_head_to_head(hd, ad)
        rec = {"home": g["home"], "away": g["away"],
               "home_composite": round(h2h.get("home_composite", 0), 2),
               "away_composite": round(h2h.get("away_composite", 0), 2),
               "margin": h2h.get("differential"), "total": h2h.get("total"),
               "home_proj": h2h.get("home_proj"), "away_proj": h2h.get("away_proj"),
               "neutral": g.get("neutral")}
        out["games"].append(rec)
        print(f"  {rec['away']:<22} @ {rec['home']:<22} "
              f"proj {rec['away_proj']:>5} - {rec['home_proj']:<5}  "
              f"margin {rec['margin']:+.1f}  total {rec['total']:.1f}  "
              f"(comp {rec['away_composite']:.1f} v {rec['home_composite']:.1f})")
    dest = OUT / f"preflight_v61_{args.tag}.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[preflight] written {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())