#!/usr/bin/env python3
"""build_season_baselines.py — assemble per-season week-1 priors for multi-year backtesting.

For season Y the "week 1 prior" is LEAK-FREE BY CONSTRUCTION:

  * season **Y-1 FINAL** ratings and efficiency (sp+, elo, fpi, srs, success rate, EPA, PPO,
    line yards, stuff rate, points/possession) — all past data;
  * season **Y OFFSEASON** facts (talent composite, recruiting class, returning production) —
    all known before kickoff;
  * and nothing at all from season Y's own games.

That is the same relationship the live model already has with SRS (served from the previous
season), so a multi-year backtest reproduces the kind of prior production actually runs on.

Field mapping mirrors the live pipeline exactly (app.py `_cfbd_advanced_for_teams`,
`_cfbd_drives_for_teams`), so a baseline value and a live value mean the same thing:

    successRate          -> off_success_rate / def_success_rate
    ppa                  -> epa_play / def_epa_play
    pointsPerOpportunity -> off_ppo / def_ppo
    lineYards            -> off_line_yards / def_line_yards
    stuffRate            -> off_stuff_rate / def_stuff_rate
    drives               -> pts_per_poss / def_pts_per_poss

Usage:
    python scripts/build_season_baselines.py --seasons 2021 2022 2023 2024 2025 2026
    python scripts/build_season_baselines.py --seasons 2026 --refresh
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CACHE = ROOT / "data" / "backtest_cache"
REFRESH = False

BASE = "https://api.collegefootballdata.com"


def _key() -> str:
    k = os.environ.get("CFBD_API_KEY")
    if not k:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("CFBD_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ["CFBD_API_KEY"] = k
                    break
    if not k:
        raise SystemExit("CFBD_API_KEY not set (and not found in repo .env)")
    return k


def cfbd(path: str, **params):
    """Fetch a CFBD endpoint, cached on disk (one fetch, many experiments)."""
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode(params)
    url = f"{BASE}/{path}" + (f"?{qs}" if qs else "")
    name = (path + "_" + qs).replace("/", "_").replace("&", "_").replace("=", "")
    fp = CACHE / f"raw_{name}.json"
    if fp.exists() and not REFRESH:
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _key()})
    data = json.load(urllib.request.urlopen(req, timeout=120))
    CACHE.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data), encoding="utf-8")
    print(f"    fetched {path} {qs} -> {len(data) if isinstance(data, list) else '?'} rows")
    return data


def _clamp100(x: float) -> float:
    return max(0.0, min(100.0, x))


def build(season: int, with_drives: bool = True) -> dict:
    """Build the week-1 prior map for `season` (keyed by CFBD team name)."""
    prior = season - 1
    print(f"  season {season}: prior = {prior} finals + {season} offseason")

    sp = {r["team"]: r for r in cfbd("ratings/sp", year=prior)}
    elo = {r["team"]: r for r in cfbd("ratings/elo", year=prior)}
    # SRS is included only so the baseline faithfully reproduces the CURRENT config; per Jeff
    # it is no longer a focus and no further SRS work should be built on it.
    srs = {r["team"]: r for r in cfbd("ratings/srs", year=prior)}
    fpi = {r["team"]: r for r in cfbd("ratings/fpi", year=prior)}
    adv = {r["team"]: r for r in cfbd("stats/season/advanced", year=prior)}
    talent = {r["team"]: r for r in cfbd("talent", year=season)}
    rec = {r["team"]: r for r in cfbd("recruiting/teams", year=season)}
    ret = {r["team"]: r for r in cfbd("player/returning", year=season)}

    ppp = {}
    if with_drives:
        drives = cfbd("drives", year=prior)
        off_d, def_d = {}, {}
        for d in drives:
            off_d.setdefault(d.get("offense", ""), []).append(d)
            def_d.setdefault(d.get("defense", ""), []).append(d)
        for team, ds in off_d.items():
            pts = sum(max(0, d.get("endOffenseScore", 0) - d.get("startOffenseScore", 0))
                      for d in ds if d.get("scoring"))
            ppp.setdefault(team, {})["pts_per_poss"] = round(pts / max(1, len(ds)), 2)
        for team, ds in def_d.items():
            pts = sum(max(0, d.get("endOffenseScore", 0) - d.get("startOffenseScore", 0))
                      for d in ds if d.get("scoring"))
            ppp.setdefault(team, {})["def_pts_per_poss"] = round(pts / max(1, len(ds)), 2)

    teams = {r.get("school") or r.get("team") for r in cfbd("teams", year=season)}
    names = ({n for n in set(sp) | set(elo) | set(fpi) | set(adv) | set(talent)
              | set(rec) | set(ret) | teams if n})
    out: dict[str, dict] = {}
    for n in sorted(names):
        s, e, f, a = sp.get(n, {}), elo.get(n, {}), fpi.get(n, {}), adv.get(n, {})
        off, dfn = a.get("offense") or {}, a.get("defense") or {}
        t, rc, rt = talent.get(n, {}), rec.get(n, {}), ret.get(n, {})
        fpi_val = f.get("fpi")
        row = {
            "name": n,
            # --- ratings: prior-season finals ---
            "sp_plus": s.get("rating"),
            "sp_rank": s.get("ranking"),
            "sp_offense": (s.get("offense") or {}).get("rating"),
            "sp_defense": (s.get("defense") or {}).get("rating"),
            "elo": e.get("elo"),
            "srs": srs.get(n, {}).get("rating"),
            "fpi": fpi_val,
            "fpi_win_prob": (round(_clamp100(50 + fpi_val * 1.5), 1)
                             if fpi_val is not None else None),
            # --- offseason facts for season Y (known before kickoff) ---
            "talent_score": t.get("talent"),
            "recruiting_rank": rc.get("rank"),
            "recruiting_pts": rc.get("points"),
            "pct_ppa_returning": (round(rt.get("percentPPA") * 100, 1)
                                  if rt.get("percentPPA") is not None else None),
            "returning_ppa": (round(rt.get("totalPPA"), 1)
                              if rt.get("totalPPA") is not None else None),
            # --- efficiency: prior-season finals (live mapping, app.py) ---
            "off_success_rate": off.get("successRate"),
            "def_success_rate": dfn.get("successRate"),
            "epa_play": off.get("ppa"),
            "def_epa_play": dfn.get("ppa"),
            "off_ppo": off.get("pointsPerOpportunity"),
            "def_ppo": dfn.get("pointsPerOpportunity"),
            "off_line_yards": off.get("lineYards"),
            "def_line_yards": dfn.get("lineYards"),
            "off_stuff_rate": off.get("stuffRate"),
            "def_stuff_rate": dfn.get("stuffRate"),
            "pts_per_poss": ppp.get(n, {}).get("pts_per_poss"),
            "def_pts_per_poss": ppp.get(n, {}).get("def_pts_per_poss"),
        }
        out[n] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build per-season week-1 priors (leak-free).")
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--refresh", action="store_true", help="re-fetch cached CFBD payloads")
    ap.add_argument("--skip-drives", action="store_true",
                    help="omit the drive-derived points/possession pair (big payload)")
    args = ap.parse_args()

    if args.refresh:
        globals()["REFRESH"] = True
    CACHE.mkdir(parents=True, exist_ok=True)

    for season in args.seasons:
        data = build(season, with_drives=not args.skip_drives)
        # Integrity guard: a week-1 prior must never be empty or degenerate.
        filled = [t for t in data.values() if t.get("sp_plus") is not None]
        if not filled:
            raise SystemExit(f"season {season}: no teams with sp_plus — refusing to write")
        out = CACHE / f"preseason_{season}.json"
        out.write_text(json.dumps(data, indent=1), encoding="utf-8")
        prov = {
            "season": season,
            "ratings_source_season": season - 1,
            "efficiency_source_season": season - 1,
            "offseason_facts_season": season,
            "points_per_possession_source_season": season - 1 if not args.skip_drives else None,
            "leak_rule": ("season Y-1 finals + season Y offseason facts only; never season Y "
                          "game data"),
            "teams": len(data),
            "teams_with_sp_plus": len(filled),
        }
        (CACHE / f"preseason_{season}.provenance.json").write_text(
            json.dumps(prov, indent=2), encoding="utf-8")
        cov = {k: sum(1 for t in data.values() if t.get(k) is not None) for k in
               ("sp_plus", "elo", "fpi_win_prob", "talent_score", "pct_ppa_returning",
                "off_success_rate", "off_ppo", "off_line_yards", "off_stuff_rate",
                "pts_per_poss")}
        print(f"    wrote {out.name}: {len(data)} teams; coverage " +
              " ".join(f"{k}={v}" for k, v in cov.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())