#!/usr/bin/env python3
"""massey_fcs.py — scrape Massey's FCS ratings into D1.

WHY
CFBD's /ratings/massey returns 0 rows, so the project had no FCS strength source
at all. `project_score_multi_factor` therefore falls back to a flat
`fcs_composite = 16.0` for EVERY FCS team, which makes FBS-vs-FCS games in the
early weeks meaningless (North Dakota State and a bottom-tier FCS school get the
identical projection). This module supplies the missing ordering.

HOW (and what the numbers are NOT)
The HTML page is Cloudflare-challenged and client-rendered, but the JSON endpoint
behind it is not gated — a plain HTTP fetch works, so no browser is needed.

The payload's advertised column titles are NOT trustworthy as scales: its "CMP"
ranges 77..1106 while Massey's own RENDERED table gives the same teams a
composite rank of 1..128, and no column reproduces that rank under any monotone
transform (best positional match 5/128 — noise). What IS reliable is the ORDER:
sorting on the `Consensus` column puts Montana St first, which the rendered table
independently confirms as the #1 FCS team.

So this module stores a RANK, not a rating:
  * massey_fcs_rank      — 1..N ordering derived by sorting on `Consensus`
  * massey_fcs_consensus — the raw Consensus value, kept for auditability

The LEVEL (what composite a given FCS rank is worth) is deliberately NOT decided
here. That is a model calibration and is fitted separately from real FBS-vs-FCS
results — see scripts/calibrate_fcs_prior.py. Keeping ordering and level apart is
what stops an external ranking's unknown scale from silently becoming a strength
score.

USAGE
  python scripts/massey_fcs.py --probe     # fetch + map, write nothing
  python scripts/massey_fcs.py --sync      # fetch + map + write to D1
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import cfbd_shared  # noqa: E402
import d1_store  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
ENDPOINT = "https://masseyratings.com/json/ranks.php"
FCS_SUB = 11605          # Massey's FCS sub id (cf/ncaa-d1 is the FBS board)
SOURCE = "massey"
ALIAS_FILE = os.path.join(_ROOT, "data", "massey_aliases.json")
MIN_EXPECTED_ROWS = 100  # refuse to write a short/partial payload

# Massey abbreviates school names ("Montana St", "E Washington", "SF Austin").
# Expansion happens BEFORE normalization so both sides compare on full words.
_ABBREV = [
    (r"\bSt\b", "state"), (r"\bChr\b", "christian"), (r"\bE\b", "eastern"),
    (r"\bW\b", "western"), (r"\bN\b", "northern"), (r"\bS\b", "southern"),
    (r"\bC\b", "central"), (r"\bNC\b", "north carolina"), (r"\bSC\b", "south carolina"),
    (r"\bSF\b", "stephen f"), (r"\bArk\b", "arkansas"), (r"\bTenn\b", "tennessee"),
    (r"\bColo\b", "colorado"), (r"\bMiss\b", "mississippi"), (r"\bCal\b", "california"),
    (r"\bSUNY\b", ""), (r"\bA&M\b", "am"), (r"\bAM\b", "am"),
    (r"\bSt\.\b", "state"), (r"\bIntl\b", "international"), (r"\bSo\b", "southern"),
    (r"\bNo\b", "northern"), (r"\bIll\b", "illinois"), (r"\bConn\b", "connecticut"),
    (r"\bMass\b", "massachusetts"), (r"\bTex\b", "texas"), (r"\bNW\b", "northwestern"),
    (r"\bNE\b", "northeastern"), (r"\bSE\b", "southeastern"), (r"\bSW\b", "southwestern"),
    (r"\bTX\b", "texas"), (r"\bMS\b", "mississippi"), (r"\bTN\b", "tennessee"),
    (r"\bCent\b", "central"), (r"\bUniv\b", ""),
]

# School-type suffixes that Massey and CFBD disagree about ("Nicholls St" vs plain
# "Nicholls"; "Montana St" vs "Montana State"). Both sides get the stem, so the
# mismatch resolves in either direction instead of needing a hand alias each time.
_STEM_SUFFIXES = ("state", "university", "college", "polytechnic")


def _variants(s: str) -> list[str]:
    out = [s]
    for suf in _STEM_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf) + 2:
            out.append(s[: -len(suf)])
    return out


def _norm(name: str) -> str:
    s = (name or "").lower()
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "", s)
    s = re.sub(r"^the", "", s)          # "The Citadel" -> "citadel"
    return s


# ── fetch ────────────────────────────────────────────────────────────────────

def fetch_fcs(season_label: str = "cf") -> list[dict]:
    """Fetch and parse Massey's FCS ratings board.

    Fails loudly on a short/reshaped payload — a silently-empty scrape would look
    exactly like 'no FCS games this week' downstream.
    """
    url = f"{ENDPOINT}?s={season_label}&sub={FCS_SUB}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://masseyratings.com/ranks",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))

    rows = payload.get("DI") or []
    cols = payload.get("CI") or []
    if len(rows) < MIN_EXPECTED_ROWS:
        raise RuntimeError(f"massey payload too small: {len(rows)} rows (CI={len(cols)})")

    titles = [c.get("title") for c in cols]
    try:
        i_team, i_conf, i_wl, i_cons = 0, 1, 2, 5
    except Exception:  # pragma: no cover
        raise RuntimeError(f"unexpected massey columns: {titles}")

    out = []
    for r in rows:
        def cell(i):
            v = r[i] if i < len(r) else None
            return v[0] if isinstance(v, list) else v

        name = cell(i_team)
        cons = cell(i_cons)
        if not name or not isinstance(cons, (int, float)):
            continue
        out.append({
            "massey_team": str(name).strip(),
            "conf": cell(i_conf),
            "wl": cell(i_wl),
            "consensus": float(cons),
        })
    if len(out) < MIN_EXPECTED_ROWS:
        raise RuntimeError(f"massey parse yielded too few usable rows: {len(out)}")
    return out


def derive_ranks(ratings: list[dict]) -> list[dict]:
    """Add `fcs_rank`: ordering by Consensus ascending.

    Ties are broken by name so the ranking is deterministic run-to-run — a
    wobbling rank would look like team improvement in the archive.
    """
    ordered = sorted(ratings, key=lambda x: (x["consensus"], x["massey_team"]))
    for i, r in enumerate(ordered, 1):
        r["fcs_rank"] = i
    return ordered


# ── team mapping ─────────────────────────────────────────────────────────────

def _alias_map() -> dict:
    if os.path.exists(ALIAS_FILE):
        with open(ALIAS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_matcher(year: int) -> dict:
    """{normalized_name_variant: team_id}.

    Built from `team_aliases()` (already name->id, and alias-aware for exactly the
    kind of divergence Massey shows) plus canonical `school` names, with
    school-type stems indexed so "Nicholls St"/"Nicholls" both resolve.
    """
    idx: dict[str, int] = {}

    def add(name, tid):
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return
        for v in _variants(_norm(name)):
            idx.setdefault(v, tid)

    for name, tid in (cfbd_shared.team_aliases(year) or {}).items():
        add(name, tid)
    for name, t in (cfbd_shared.teams_by_name(year) or {}).items():
        if t.get("id"):
            add(name, t["id"])
    return idx


def match_team(massey_name: str, idx: dict, aliases: dict) -> int | None:
    hit = aliases.get(massey_name) or aliases.get(_norm(massey_name))
    if hit:
        return int(hit)
    for v in _variants(_norm(massey_name)):
        if v in idx:
            return idx[v]
    cand = difflib.get_close_matches(_norm(massey_name), list(idx.keys()), n=1, cutoff=0.90)
    if cand:
        return idx[cand[0]]
    return None


def map_teams(ratings: list[dict], year: int) -> tuple[list[dict], list[str]]:
    idx = build_matcher(year)
    aliases = _alias_map()
    mapped, unmatched = [], []
    for r in ratings:
        tid = match_team(r["massey_team"], idx, aliases)
        if tid is None:
            unmatched.append(r["massey_team"])
            continue
        r["subject_id"] = int(tid)
        mapped.append(r)
    return mapped, unmatched


# ── write ────────────────────────────────────────────────────────────────────

def sync(season: int, week: int = 0, season_label: str = "cf") -> dict:
    """Fetch, map and write one Massey snapshot.

    `week` defaults to 0 = SEASON-LEVEL snapshot, matching how season aggregates
    are stored, so the model reads "the latest row for the season" without having
    to know Massey's calendar. Pass a CFBD week explicitly to keep a per-week
    history (the key is (team, season, stat_key, week), so snapshots accumulate).

    NOTE the snapshot is CURRENT-form: running this in week 1 and using the result
    for a week-1 game is correct, but re-running it mid-season and applying the
    new numbers retroactively would be lookahead. Callers must read the snapshot
    that was current when the game was played.
    """
    ratings = derive_ranks(fetch_fcs(season_label))
    mapped, unmatched = map_teams(ratings, season)
    wk = int(week)

    rows = []
    for r in mapped:
        rows.append({"subject_type": "team", "subject_id": r["subject_id"], "season": season,
                     "week": wk, "stat_key": "massey_fcs_rank", "value": float(r["fcs_rank"]),
                     "source": SOURCE})
        rows.append({"subject_type": "team", "subject_id": r["subject_id"], "season": season,
                     "week": wk, "stat_key": "massey_fcs_consensus", "value": float(r["consensus"]),
                     "source": SOURCE})

    written = 0
    if rows:
        written = d1_store.upsert_stat_observations(rows)
    return {"fetched": len(ratings), "mapped": len(mapped), "unmatched": unmatched,
            "rows": len(rows), "written": written, "week": wk}


def main() -> int:
    ap = argparse.ArgumentParser(description="Massey FCS ratings -> D1")
    ap.add_argument("--sync", action="store_true", help="write to D1")
    ap.add_argument("--probe", action="store_true", help="fetch + map only, write nothing")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--week", type=int, default=0,
                    help="0 = season-level snapshot (default); set a CFBD week to keep per-week history")
    ap.add_argument("--label", default="cf", help="Massey season label (default cf = current)")
    args = ap.parse_args()

    season = args.season or cfbd_shared.CFBD_YEAR

    if args.probe or not args.sync:
        ratings = derive_ranks(fetch_fcs(args.label))
        mapped, unmatched = map_teams(ratings, season)
        print(f"fetched {len(ratings)} FCS rows; mapped {len(mapped)}; unmatched {len(unmatched)}")
        print("\ntop 12 by derived FCS rank:")
        for r in mapped[:12]:
            print(f"  #{r['fcs_rank']:>3}  {r['massey_team']:<18} {r['conf']:<8} "
                  f"{r['wl']:<5} id={r['subject_id']}")
        if unmatched:
            print(f"\nUNMATCHED ({len(unmatched)}) — add to {os.path.relpath(ALIAS_FILE, _ROOT)}:")
            for n in unmatched:
                print("   ", n)
        return 0

    res = sync(season, args.week, args.label)
    print(json.dumps({k: v for k, v in res.items() if k != "unmatched"}, indent=2))
    if res["unmatched"]:
        print(f"unmatched ({len(res['unmatched'])}): {', '.join(res['unmatched'])}")
    return 0 if res["written"] else 1


if __name__ == "__main__":
    sys.exit(main())