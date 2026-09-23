"""backtest_rig.py — the standing backtesting entry point.

Run an EXPERIMENT: a set of named weightings (arms), evaluated over identical point-in-time
data, reported side by side, and ARCHIVED so the result can be re-read months later.

    python scripts/backtest_rig.py --list
    python scripts/backtest_rig.py                      # every arm in data/backtest_arms.json
    python scripts/backtest_rig.py --arms baseline,zero-srs
    python scripts/backtest_rig.py --refresh            # re-fetch CFBD inputs first
    python scripts/backtest_rig.py --no-d1              # local archive only

Why a rig and not a script run by hand
--------------------------------------
The harness could always produce a number; nothing gave the number a home. Every
experiment was ephemeral, so "did this weighting help?" was unanswerable over time. So:

  * inputs are CACHED (data/backtest_cache) — one fetch, many experiments, and every arm
    provably evaluates the SAME bytes;
  * an arm is DATA (data/backtest_arms.json), not a shell invocation;
  * each arm's weights are validated to total 1.0 BEFORE running, so a miscalibrated arm
    cannot produce confident garbage;
  * results are archived per run: JSONL + run directory locally, and a row per arm in the
    D1 `backtest_runs` table keyed by the composite hash (model_version).

Safety
------
Never writes to the published artifacts (data/backtest_summary.json,
data/backtest_fixture_2026.json) — those are the reviewed baseline of record. Never changes
the model: weights are applied through COMPOSITE_WEIGHTS_JSON for the duration of a run only.
This tool MEASURES; it does not decide.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DATA = ROOT / "data"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

ARMS_FILE = DATA / "backtest_arms.json"
RUNS_DIR = DATA / "backtest_runs"
RUNS_JSONL = DATA / "backtest_runs.jsonl"
SCHEMA_FILE = ROOT / "d1" / "schema.sql"


def load_arms(selected: list[str] | None) -> dict:
    if not ARMS_FILE.exists():
        raise SystemExit(f"missing {ARMS_FILE.relative_to(ROOT)}")
    doc = json.loads(ARMS_FILE.read_text(encoding="utf-8"))
    arms = doc.get("arms") or {}
    enabled = doc.get("enabled_keys") or ["sp_plus", "fpi", "srs", "elo", "talent", "efficiency"]
    if selected:
        missing = [a for a in selected if a not in arms]
        if missing:
            raise SystemExit(f"unknown arm(s): {missing}. Known: {sorted(arms)}")
        arms = {k: v for k, v in arms.items() if k in selected}
    # Validate every arm BEFORE running anything: a weight set that does not total 1.0
    # would silently mis-calibrate the 0-100 composite, and a confident wrong number is
    # worse than no run at all.
    for name, spec in arms.items():
        w = spec.get("weights")
        if w is None:
            continue
        total = sum(float(w.get(k, 0.0)) for k in enabled)
        if abs(total - 1.0) > 1e-6:
            raise SystemExit(f"arm '{name}' enabled weights total {total:.6f}, expected 1.0")
    return arms


def run_arm(name: str, spec: dict, run_id: str) -> dict:
    """Run one arm in-process. Returns {'summary', 'stats', 'model_version', 'weights'}."""
    import run_model_backtest as hb

    weights = spec.get("weights")
    prev = os.environ.get("COMPOSITE_WEIGHTS_JSON")
    if weights is None:
        os.environ.pop("COMPOSITE_WEIGHTS_JSON", None)
    else:
        os.environ["COMPOSITE_WEIGHTS_JSON"] = json.dumps(weights)
    try:
        import app
        version = app.composite_version()
        active = app.composite_config()
        out_dir = RUNS_DIR / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = hb.run_backtest(
            summary_out=str(out_dir / f"{name}.summary.json"),
            fixture_out=str(out_dir / f"{name}.fixture.json"),
        )
    finally:
        if prev is None:
            os.environ.pop("COMPOSITE_WEIGHTS_JSON", None)
        else:
            os.environ["COMPOSITE_WEIGHTS_JSON"] = prev

    import compare_backtest_arms as cmp
    fixture = json.loads((out_dir / f"{name}.fixture.json").read_text(encoding="utf-8"))
    return {
        "summary": summary,
        "stats": cmp.arm_stats(fixture),
        "fixture_path": str(out_dir / f"{name}.fixture.json"),
        "summary_path": str(out_dir / f"{name}.summary.json"),
        "model_version": version,
        "weights": active,
    }


def ensure_d1_table() -> None:
    """Create backtest_runs if absent, straight from d1/schema.sql (one source of truth)."""
    import d1_store
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    stmts = re.findall(r"(CREATE (?:TABLE|INDEX) IF NOT EXISTS backtest_runs[\s\S]*?;)", sql)
    if not stmts:
        raise RuntimeError("could not find backtest_runs DDL in d1/schema.sql")
    for s in stmts:
        d1_store.query_full(s)


def archive_d1(rows: list[dict]) -> int | None:
    """Best-effort D1 archival. Never fail an experiment because telemetry is unavailable."""
    try:
        sys.path.insert(0, str(ROOT))
        import d1_store
        import d1_write_path
        if not d1_write_path.enabled():
            print("  [d1] skipped (D1_WRITE_ENABLED is off)")
            return None
        if not os.environ.get("CF_D1_TOKEN"):
            print("  [d1] skipped (no CF_D1_TOKEN)")
            return None
        ensure_d1_table()
        written = d1_store.append_backtest_runs(rows)
        print(f"  [d1] archived {len(rows)} arm row(s) to backtest_runs "
              f"(D1 counted {written} rows written, incl. index maintenance)")
        return len(rows)
    except Exception as e:  # noqa: BLE001
        print(f"  [d1] archive failed (experiment unaffected): {e}")
        return None


def report(results: dict, run_id: str) -> None:
    names = list(results)
    base = names[0]
    print()
    print(f"  run {run_id} — {len(names)} arm(s), identical cached inputs")
    print()
    print("  SPREADS (ATS) — FBS matchups only")
    print(f"    {'arm':<14} {'record':>10} {'pct':>7}   {'vs ' + base:>9}")
    for n in names:
        t = results[n]["summary"]["fbs_ats_tiers"]["all_fbs_picks"]
        d = t["win_pct"] - results[base]["summary"]["fbs_ats_tiers"]["all_fbs_picks"]["win_pct"]
        print(f"    {n:<14} {t['record']:>10} {t['win_pct']:>6.1f}%   {d:>+8.1f}")
    print()
    print("  TOTALS (O/U) — FBS matchups only")
    print(f"    {'arm':<14} {'record':>10} {'pct':>7}   {'vs ' + base:>9}")
    for n in names:
        t = results[n]["summary"]["fbs_totals_tiers"]["all_totals"]
        d = t["win_pct"] - results[base]["summary"]["fbs_totals_tiers"]["all_totals"]["win_pct"]
        print(f"    {n:<14} {t['record']:>10} {t['win_pct']:>6.1f}%   {d:>+8.1f}")
    print()
    print("  DIAGNOSTICS")
    for n in names:
        s = results[n]["stats"]
        print(f"    {n}: dogs {100*s['dogs'][0]/max(1,s['graded']):.0f}% of picks "
              f"(won {s['dogs'][3]:.1f}%) | mean |margin| {s['model_mag']:.2f} vs book "
              f"{s['book_mag']:.2f} | MAE {s['mae']:.2f} | model_version {results[n]['model_version']}")


def show_history(limit: int = 40) -> int:
    """List archived experiments, newest first. D1 first (the durable record), with the
    local JSONL as an offline fallback so a laptop with no token still sees its own runs."""
    rows = []
    try:
        import d1_store
        if os.environ.get("CF_D1_TOKEN"):
            rows = d1_store.query(
                "SELECT run_id, arm, model_version, fbs_matchups, ats_all, totals_all, "
                "dog_share_pct FROM backtest_runs ORDER BY ts_utc DESC, arm LIMIT ?",
                [limit])
    except Exception as e:  # noqa: BLE001
        print(f"  [history] D1 unavailable ({e}); falling back to local archive")
        rows = []
    if not rows and RUNS_JSONL.exists():
        local = [json.loads(l) for l in RUNS_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
        local.sort(key=lambda r: r.get("ts_utc", ""), reverse=True)
        rows = local[:limit]
    if not rows:
        print("  no archived runs yet — run the rig first")
        return 0
    print(f"  {'run_id':<20} {'arm':<12} {'version':<14} {'n':>4} {'ATS':>7} {'O/U':>7} {'dog%':>6}")
    for r in rows:
        print(f"  {str(r.get('run_id','')):<20} {str(r.get('arm','')):<12} "
              f"{str(r.get('model_version','')):<14} {r.get('fbs_matchups') or 0:>4} "
              f"{r.get('ats_all') or 0:>6.1f}% {r.get('totals_all') or 0:>6.1f}% "
              f"{r.get('dog_share_pct') or 0:>5.0f}%")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Standing backtest rig (measure, never decide).")
    ap.add_argument("--arms", default=None, help="comma-separated arm names (default: all)")
    ap.add_argument("--history", action="store_true",
                    help="list archived experiments from D1 (local fallback) and exit")
    ap.add_argument("--list", action="store_true", help="list available arms and exit")
    ap.add_argument("--refresh", action="store_true", help="re-fetch cached CFBD inputs")
    ap.add_argument("--no-d1", action="store_true", help="local archive only")
    ap.add_argument("--note", default="", help="free-text note stored with the run")
    args = ap.parse_args()

    if args.history:
        return show_history()

    if args.list:
        arms = load_arms(None)
        for name, spec in arms.items():
            w = spec.get("weights")
            print(f"  {name:<16} {'defaults (live weights)' if w is None else json.dumps(w)}")
            if spec.get("note"):
                print(f"                   {spec['note']}")
        return 0

    selected = [a.strip() for a in args.arms.split(",")] if args.arms else None
    arms = load_arms(selected)

    if args.refresh:
        import run_model_backtest as hb
        hb.REFRESH = True
        print("  --refresh: re-fetching cached CFBD inputs")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"  run id: {run_id}   arms: {', '.join(arms)}")

    results = {}
    for name, spec in arms.items():
        print(f"\n  === arm: {name} ===")
        results[name] = run_arm(name, spec, run_id)

    report(results, run_id)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for name, r in results.items():
        s, st = r["summary"], r["stats"]
        ats = s["fbs_ats_tiers"]
        tot = s["fbs_totals_tiers"]
        rows.append({
            "run_id": run_id, "ts_utc": ts, "arm": name,
            "model_version": r["model_version"],
            "weights_json": json.dumps(r["weights"], sort_keys=True),
            "season": s.get("season"),
            "games": s.get("total_evaluated_games"),
            "fbs_matchups": s.get("fbs_matchups_evaluated"),
            "ats_all": ats["all_fbs_picks"]["win_pct"],
            "ats_3star": ats["tier_2_3star_3.5pt"]["win_pct"],
            "ats_5star": ats["tier_3_5star_7pt"]["win_pct"],
            "totals_all": tot["all_totals"]["win_pct"],
            "totals_3star": tot["totals_3star_4pt"]["win_pct"],
            "dog_share_pct": round(100.0 * st["dogs"][0] / max(1, st["graded"]), 1),
            "mean_model_margin": round(st["model_mag"], 3),
            "mean_book_line": round(st["book_mag"], 3),
            "mae_vs_book": round(st["mae"], 3),
            "bias_vs_book": round(st["bias"], 3),
            "note": args.note or (arms[name].get("note") or "")[:400],
        })

    with open(RUNS_JSONL, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"\n  archived: {RUNS_JSONL.relative_to(ROOT)} (+{len(rows)} rows)")
    print(f"  per-arm detail: {RUNS_DIR.relative_to(ROOT)}/{run_id}/")

    if not args.no_d1:
        archive_d1(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())