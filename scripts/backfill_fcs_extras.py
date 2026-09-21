#!/usr/bin/env python3
"""backfill_fcs_extras.py — the FCS pieces of the D1 backfill that CFBD can serve.

Probed live 2026-09-21 (receipts in logs):
  * /games with no filter ALREADY returns every classification (2025: 3,831 games,
    563 distinct non-FBS schools) — so FCS GAMES are already in `games`. What was
    missing is the DIVISION TAG, which this script adds as `teams.classification`
    (source: /teams -> classification = fbs | fcs | ii | iii). Join games->teams to
    slice by division; no destructive games rewrite.
  * `/games?division=fcs` and `/stats/season?division=fcs` SILENTLY IGNORE the filter
    (return the same FBS rows) -> FCS-specific *stats* are NOT available from CFBD.
    Those need another source; NOT attempted here (documented, not faked).
  * `/rankings` DOES return an "FCS Coaches Poll" (25 ranks/week) -> ingested here as
    stat_observations(subject_type='team', stat_key='fcs_rating', week=<poll week>,
    value=<rank>), 2021-2026.

Idempotent + additive. Writes only:
  * ALTER TABLE teams ADD COLUMN classification (skipped if present)
  * teams.classification upserts
  * stat_observations fcs_rating rows (deduped by the ux_stat_obs_subject index)

Does NOT touch data/backfill_checkpoint.json (that belongs to backfill_d1.py).
Run:  CF_D1_TOKEN=... python -u scripts/backfill_fcs_extras.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import d1_store  # noqa: E402
import cfbd_shared  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRESS = os.path.join(REPO, "data", "fcs_extras_progress.json")
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]
REG_WEEKS = list(range(1, 17))
CALLS = 0


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def cfbd(path, **kw):
    global CALLS
    CALLS += 1
    return cfbd_shared.cfbd_get(path, **kw) or []


def save(p: dict) -> None:
    with open(PROGRESS, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)


def load() -> dict:
    try:
        with open(PROGRESS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"steps": {}, "rows_written": 0}


def ensure_classification_col() -> None:
    cols = [r["name"] for r in d1_store.query("PRAGMA table_info(teams)")]
    if "classification" in cols:
        print("teams.classification already present", flush=True)
        return
    try:
        d1_store.query_full("ALTER TABLE teams ADD COLUMN classification TEXT")
        print("teams.classification ADDED", flush=True)
    except Exception as e:  # duplicate column on a concurrent run is fine
        if "duplicate column" in str(e).lower():
            print(f"teams.classification already present ({e})", flush=True)
        else:
            raise


def tag_divisions() -> int:
    """teams.classification from /teams (fbs|fcs|ii|iii)."""
    total = 0
    for season in SEASONS:
        rows = cfbd("teams", year=season)
        out = []
        for t in rows:
            if not t.get("id"):
                continue
            cls = t.get("classification") or t.get("division")
            if cls:
                out.append({"team_id": t["id"], "name": t.get("school"),
                        "classification": str(cls).lower()})
        if not out:
            continue
        for part in [out[i:i + 33] for i in range(0, len(out), 33)]:  # 33*3=99 params <= D1's 100 cap
            ph = ",".join("(?,?,?)" for _ in part)
            d1_store.assert_headroom(len(part))
            _, meta = d1_store.query_full(
                f"INSERT INTO teams (team_id, name, classification) VALUES {ph} "
                f"ON CONFLICT(team_id) DO UPDATE SET classification=excluded.classification",
                [v for r in part for v in (r["team_id"], r["name"], r["classification"])])
            ink = d1_store.confirmed_writes(meta)
            if ink <= 0:
                raise d1_store.ConfirmedWriteError(f"teams classification upsert confirmed 0 (meta={meta})")
            d1_store.commit_writes(ink)
            total += ink
    return total


def div_summary() -> str:
    rows = d1_store.query("SELECT classification AS c, COUNT(*) AS n FROM teams GROUP BY classification")
    return ", ".join(f"{r['c']}:{r['n']}" for r in rows) or "(none)"


def fcs_poll() -> int:
    """FCS Coaches Poll -> fcs_rating observations (rank), regular weeks 1-16."""
    total, calls0 = 0, CALLS
    for season in SEASONS:
        obs = []
        for wk in REG_WEEKS:
            polls = cfbd("rankings", year=season, week=wk, seasonType="regular")
            for entry in polls or []:
                for p in (entry.get("polls") or []):
                    if p.get("poll") != "FCS Coaches Poll":
                        continue
                    for r in (p.get("ranks") or []):
                        if r.get("teamId") is None or r.get("rank") is None:
                            continue
                        obs.append({"subject_type": "team", "subject_id": r["teamId"],
                                    "season": season, "week": wk, "stat_key": "fcs_rating",
                                    "value": float(r["rank"]), "source": "cfbd",
                                    "recorded_at": _ts()})
        if obs:
            n = d1_store.upsert_stat_observations(obs)
            total += n
            print(f"  FCS Coaches Poll {season}: {len(obs)} obs -> {n} confirmed writes", flush=True)
        else:
            print(f"  FCS Coaches Poll {season}: no poll rows", flush=True)
    print(f"  poll cfbd calls: {CALLS - calls0}", flush=True)
    return total


def main() -> int:
    p = load()
    print(f"FCS extras start {_ts()}  (progress {PROGRESS})", flush=True)
    if not p["steps"].get("classification_col"):
        ensure_classification_col()
        p["steps"]["classification_col"] = True
        save(p)
    if not p["steps"].get("divisions"):
        n = tag_divisions()
        p["rows_written"] += n
        p["steps"]["divisions"] = True
        p["division_counts"] = div_summary()
        save(p)
        print(f"divisions tagged: {n} confirmed writes | {p['division_counts']}", flush=True)
    else:
        print(f"divisions already tagged | {div_summary()}", flush=True)
    if not p["steps"].get("fcs_poll"):
        n = fcs_poll()
        p["rows_written"] += n
        p["steps"]["fcs_poll"] = True
        save(p)
        print(f"FCS Coaches Poll backfill: {n} confirmed writes", flush=True)
    p["finished_at"] = _ts()
    p["cfbd_calls"] = CALLS
    save(p)
    print(f"DONE fcs extras: rows_written={p['rows_written']} cfbd_calls={CALLS} "
          f"d1_today={d1_store.ledger_written()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())