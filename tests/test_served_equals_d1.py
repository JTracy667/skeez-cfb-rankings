"""PARITY — what the site SERVES must equal what D1 STORES. Fails loudly on drift.

WHY THIS EXISTS
The v50 defect was not that a bug existed; it was that nothing FAILED. The weekly pull
wrote fresh numbers to D1 and the site served a file baked into the image, and no test,
no monitor and no health check noticed for four days. A written trap is a note. This is
the control: if served and stored disagree, the suite goes red and the deploy gate stops.

HOW IT IS ORGANISED
  * STRICT tests   -- must pass on every run. They are the regression tripwire.
  * xfail(strict=True) tests -- the KNOWN-BROKEN state, named with its finding id.
    `strict=True` means: if one of these starts PASSING, pytest reports a FAILURE
    ("XPASS(strict)"). That is the signal to delete the marker -- so the worklist cannot
    silently rot into a false sense of coverage. Each Phase removes its own xfail.

D1 is read with `d1_store.query` (SELECT only), so this suite needs no write capability.

Run:  python -m pytest tests/test_served_equals_d1.py -v
"""
import os

import pytest

os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
os.environ.setdefault("D1_WRITE_ENABLED", "1")

SEASON = 2026
TOL = 0.051          # stored values are rounded to 2dp
MIN_TEAMS = 12       # a vacuous "0 comparisons" pass is not a pass


def _d1():
    import d1_store  # noqa: PLC0415
    return d1_store


@pytest.fixture(scope="module", autouse=True)
def _require_d1():
    if not os.environ.get("CF_D1_TOKEN"):
        pytest.skip("CF_D1_TOKEN not set - parity cannot be verified. The deploy gate "
                    "must run WITH D1 credentials; a skip here must never be read as a pass.")


@pytest.fixture(scope="module")
def app_module():
    import app  # noqa: PLC0415
    return app


@pytest.fixture(scope="module")
def served(app_module):
    from fastapi.testclient import TestClient  # noqa: PLC0415
    client = TestClient(app_module.app)
    return {
        "rankings": client.get("/api/rankings").json(),
        "analytics": client.get("/api/analytics").json(),
    }


def _newest_team_week():
    d = _d1()
    rows = d.query(
        "SELECT MAX(week) w FROM stat_observations "
        "WHERE season=? AND subject_type='team'", [SEASON])
    return rows[0]["w"] if rows else None


def _d1_metric_map(keys, week):
    """{(team_name, stat_key): value} in ONE D1 query -- a per-team loop is 50 round trips."""
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = _d1().query(
        f"SELECT t.name n, o.stat_key k, o.value v FROM stat_observations o "
        f"JOIN teams t ON t.team_id=o.subject_id "
        f"WHERE o.season=? AND o.week=? AND o.subject_type='team' AND o.stat_key IN ({marks})",
        [SEASON, week, *keys])
    return {(r["n"], r["k"]): r["v"] for r in rows}


def _d1_metric(name, key, week):
    rows = _d1().query(
        "SELECT o.value v FROM stat_observations o JOIN teams t ON t.team_id=o.subject_id "
        "WHERE o.season=? AND o.week=? AND o.subject_type='team' AND o.stat_key=? "
        "AND t.name=? LIMIT 1", [SEASON, week, key, name])
    return rows[0]["v"] if rows else None


# --------------------------------------------------------------------------- STRICT


def test_served_analytics_equals_d1_newest(served):
    """Team analytics served on the site must equal D1's newest archived values."""
    week = _newest_team_week()
    assert week is not None, "no team rows in stat_observations - D1 has no analytics at all"

    stored_map = _d1_metric_map(("sp_plus", "srs"), week)
    checked, bad = 0, []
    # NB: must be the ANALYTICS payload -- /api/rankings is served from a D1 slate board,
    # so it cannot observe the analytics source at all (learned the hard way 2026-09-28).
    for t in served["analytics"]["teams"][:40]:
        for key in ("sp_plus", "srs"):
            if t.get(key) is None:
                continue
            stored = stored_map.get((t["name"], key))
            if stored is None:
                continue
            checked += 1
            if abs(float(t[key]) - float(stored)) > TOL:
                bad.append(f"{t['name']} {key}: served {t[key]} != D1 {stored} (week {week})")

    assert checked >= MIN_TEAMS, (
        f"only {checked} (team, metric) pairs were comparable - parity is not being "
        "verified. Check the D1 join and the served payload shape, do not loosen this.")
    assert not bad, "served != stored for:\n" + "\n".join(f"  - {b}" for b in bad)


def test_rankings_board_is_sorted_by_composite_rank(served):
    """Jeff's rule: the rankings page is ALWAYS ordered by composite rank."""
    teams = served["rankings"]["teams"]
    assert len(teams) == 25, f"expected the top-25 board, got {len(teams)}"
    ranks = [t["rank"] for t in teams]
    assert ranks == list(range(1, 26)), f"rank column is not 1..25: {ranks[:8]}..."
    comps = [float(t["composite"]) for t in teams]
    assert comps == sorted(comps, reverse=True), (
        "the board is not ordered by composite descending - AP/Coaches columns must never "
        "drive the sort (Jeff corrected this twice)")


@pytest.mark.xfail(strict=True,
                   reason="F3: D1 `teams` != the served universe (Phase 6). Measured "
                          "2026-09-28: Anna Maria College, Defiance College")
def test_served_universe_covers_d1_teams(served):
    """Every team D1 knows must be servable. A team D1 has but the site cannot render is
    exactly how a dataset drifts out of sync unnoticed (F3)."""
    d1_ids = {r["name"] for r in _d1().query("SELECT name FROM teams")}
    served_names = {t["name"] for t in served["analytics"]["teams"]}
    missing = sorted(d1_ids - served_names)
    assert not missing, (
        f"{len(missing)} team(s) exist in D1 `teams` but cannot be served:\n"
        + "\n".join(f"  - {n}" for n in missing[:15])
        + "\n\nD1 `teams` has no live writer (F3): the served identity comes from the "
          "ephemeral disk file. Either reconcile the two or retire the D1 copy."
    )


def test_serving_follows_d1_even_when_the_disk_file_disagrees(app_module, served, tmp_path, monkeypatch):
    """THE test that proves D1 is the source -- a counterfactual, built in.

    A parity check alone cannot distinguish "reading D1" from "reading disk" when the two
    happen to hold the same numbers (the disk file was regenerated FROM D1 in v50, so they
    agree by construction and the check passed vacuously). So force them apart: rewrite the
    on-disk file with a sentinel and assert the site serves D1's value anyway.

    This is the v50 defect as an executable assertion.
    """
    import json  # noqa: PLC0415

    from fastapi.testclient import TestClient  # noqa: PLC0415

    week = _newest_team_week()
    teams = served["analytics"]["teams"]
    d1_sp = _d1_metric_map(("sp_plus",), week)
    sample = [t["name"] for t in teams if (t["name"], "sp_plus") in d1_sp][:25]
    assert len(sample) >= MIN_TEAMS, f"only {len(sample)} teams have a D1 sp_plus at week {week}"

    stale = tmp_path / "cfbd_analytics.json"
    real = app_module._CFBD_ANALYTICS_FILE
    doc = json.loads(real.read_text(encoding="utf-8"))
    rows = doc["teams"] if isinstance(doc, dict) else doc
    poisoned = 0
    for t in rows:
        if isinstance(t, dict) and t.get("name") in sample and isinstance(t.get("sp_plus"), (int, float)):
            t["sp_plus"] = -999.0
            poisoned += 1
    stale.write_text(json.dumps(doc), encoding="utf-8")
    assert poisoned >= MIN_TEAMS, f"only poisoned {poisoned} rows"

    monkeypatch.setattr(app_module, "_CFBD_ANALYTICS_FILE", stale)
    monkeypatch.setenv("ANALYTICS_FROM_D1", "1")
    got = TestClient(app_module.app).get("/api/analytics").json()["teams"]
    by_name = {t["name"]: t for t in got}

    followed_d1, followed_disk = [], []
    for name in sample:
        served_v = by_name.get(name, {}).get("sp_plus")
        d1_v = d1_sp[(name, "sp_plus")]
        if served_v is None:
            continue
        (followed_d1 if abs(float(served_v) - float(d1_v)) <= TOL else followed_disk).append(
            f"{name}: served {served_v}, D1 {d1_v}")

    assert not followed_disk, (
        "serving followed the EPHEMERAL DISK FILE instead of D1 -- this is the v50 defect:\n"
        + "\n".join(f"  - {x}" for x in followed_disk[:10]))
    assert len(followed_d1) >= MIN_TEAMS, (
        f"only {len(followed_d1)} teams verified against D1 ({len(sample)} sampled)")


def test_live_finals_fetch_archives_to_d1(app_module, monkeypatch):
    """The LIVE results path must ARCHIVE what it fetches.

    Without this, fixing F1 once by backfill is a lie: D1 goes stale again the moment the
    live path grades a new weekend into a file nobody durable ever reads. No network and no
    production write -- the data-layer door is stubbed so we assert the CALL.
    """
    fake = [{"id": 1, "season": SEASON, "week": 9, "homeId": 11, "awayId": 22,
             "homeTeam": "Alpha", "awayTeam": "Beta", "homePoints": 31, "awayPoints": 17,
             "completed": True, "startDate": "2026-10-31T16:00:00.000Z",
             "venue": "Test Field", "neutralSite": False}]
    seen: dict = {}
    monkeypatch.setattr(app_module.d1_write_path, "snapshot_games",
                        lambda games: seen.setdefault("games", games) and 0 or len(games))
    monkeypatch.setattr(app_module, "_http_get", lambda *a, **k: fake)
    monkeypatch.delenv("CFB_SKIP_LIVE_FETCH", raising=False)   # force the live branch
    app_module._FINALS_CACHE["data"], app_module._FINALS_CACHE["ts"] = {}, 0

    out = app_module._fetch_final_scores(SEASON)

    assert seen.get("games") == fake, (
        "the live finals fetch did NOT archive to D1 -- results would go stale again (F1)")
    assert any(v.get("home_score") == 31 for v in out.values()), (
        "the fetched finals were not graded into the returned map")


def test_schedule_override_is_durable_d1_first(app_module, monkeypatch):
    """F2: the admin schedule override must survive a container recycle.

    `POST /api/schedule/update` used to be a read-modify-write on `data/week_schedule.json`
    -- an EPHEMERAL disk -- so a manual override silently evaporated and the site reverted
    to the baked copy. D1 is the store now; prove it by making D1 and the disk disagree and
    asserting D1 wins. No production write: the state read is stubbed.
    """
    import json  # noqa: PLC0415

    sentinel = {"week": 99, "season": SEASON, "updated": "from-d1",
                "matchups": [{"home": "D1 Home", "away": "D1 Away"}]}
    monkeypatch.setattr(app_module.d1_write_path, "load_state",
                        lambda key: json.dumps(sentinel) if key == "week_schedule" else None)

    got = app_module.load_schedule()
    assert got.get("updated") == "from-d1", f"D1 did not win over disk: {got}"
    assert got["matchups"][0]["home"] == "D1 Home"


def test_schedule_falls_back_to_disk_when_d1_has_nothing(app_module, monkeypatch):
    """A local-dev run with no D1 state must still load the file rather than crash."""
    monkeypatch.setattr(app_module.d1_write_path, "load_state", lambda key: None)
    got = app_module.load_schedule()
    assert isinstance(got, dict) and "matchups" in got


def test_budget_ledger_reads_d1_first_and_falls_back_to_the_file(monkeypatch):
    """Phase 6: D1 `api_usage` is the ledger of record; the disk mirror can only be staler.

    `flush()` writes the file and D1 in the SAME call, so the file is never ahead of D1 --
    and on the container it lives on an EPHEMERAL disk, exactly the stale-read class that hid
    the analytics bug. Make the two disagree and assert D1 wins.
    """
    import budget  # noqa: PLC0415

    d1_truth = {"schema": 1, "day": {"2026-09-28": {"cfbd": {"calls": 4321}}},
                "month": {"2026-09": {"cfbd": {"calls": 4321}}}, "paused": {}}
    stale_file = {"schema": 1, "day": {"2026-09-28": {"cfbd": {"calls": 7}}},
                  "month": {"2026-09": {"cfbd": {"calls": 7}}}, "paused": {}}

    monkeypatch.setattr(budget, "_d1_load", lambda: d1_truth)
    monkeypatch.setattr(budget, "_read_file", lambda: stale_file)
    monkeypatch.setattr(budget, "_state", None)
    assert budget.state()["day"]["2026-09-28"]["cfbd"]["calls"] == 4321, \
        "the stale disk mirror won over D1"

    # D1 has nothing -> the file is the fallback (local dev / D1 unreachable).
    monkeypatch.setattr(budget, "_state", None)
    monkeypatch.setattr(budget, "_d1_load", lambda: None)
    assert budget.state()["day"]["2026-09-28"]["cfbd"]["calls"] == 7

    # Kill switch: with BUDGET_FROM_D1=0 the file is authoritative and D1 is not consulted.
    monkeypatch.setattr(budget, "_state", None)
    monkeypatch.setenv("BUDGET_FROM_D1", "0")
    monkeypatch.setattr(budget, "_d1_load",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("D1 must not be consulted when the kill switch is off")))
    assert budget.state()["day"]["2026-09-28"]["cfbd"]["calls"] == 7
    monkeypatch.setattr(budget, "_state", None)


def test_injuries_are_d1_first_and_beat_a_disagreeing_disk(app_module, monkeypatch, tmp_path):
    """Phase 6: `POST /api/injuries/override` was a read-modify-write on an EPHEMERAL file, so
    a manual injury override silently evaporated on the next container recycle -- F2's shape
    again, but this one loses a human's correction. D1 is the store now."""
    import json  # noqa: PLC0415

    disk = tmp_path / "active_injuries.json"
    disk.write_text(json.dumps({"teams": {"Texas": {"net_injury_points": 99.0}}}), encoding="utf-8")
    monkeypatch.setattr(app_module, "ACTIVE_INJURIES_FILE", disk)

    d1_doc = {"teams": {"Texas": {"net_injury_points": -7.0}},
              "total_teams_with_injuries": 1, "total_tracked_injuries": 1}
    monkeypatch.setattr(app_module.d1_write_path, "load_state",
                        lambda key: json.dumps(d1_doc) if key == "active_injuries" else None)

    got = app_module._load_active_injuries()
    assert got["Texas"]["net_injury_points"] == -7.0, "the ephemeral file won over D1"


def test_injuries_fall_back_to_disk_when_d1_has_nothing(app_module, monkeypatch, tmp_path):
    import json  # noqa: PLC0415

    disk = tmp_path / "active_injuries.json"
    disk.write_text(json.dumps({"teams": {"Texas": {"net_injury_points": -3.0}}}), encoding="utf-8")
    monkeypatch.setattr(app_module, "ACTIVE_INJURIES_FILE", disk)
    monkeypatch.setattr(app_module.d1_write_path, "load_state", lambda key: None)

    assert app_module._load_active_injuries()["Texas"]["net_injury_points"] == -3.0


def test_injury_writes_reach_the_d1_door(app_module, monkeypatch, tmp_path):
    """A write that only lands in the file is exactly the bug. Assert the D1 door is called."""
    import json  # noqa: PLC0415

    monkeypatch.setattr(app_module, "ACTIVE_INJURIES_FILE", tmp_path / "active_injuries.json")
    saved: dict = {}
    monkeypatch.setattr(app_module.d1_write_path, "save_state",
                        lambda k, v: saved.__setitem__(k, json.loads(v)) or 1)

    app_module._save_injuries_doc({"teams": {"Texas": {"net_injury_points": -4.0}}})
    assert saved.get("active_injuries", {}).get("teams", {}).get("Texas"), \
        "the injury write never reached the D1 door"


def test_odds_cache_is_d1_first_and_durable(app_module, monkeypatch, tmp_path):
    """Phase 6: the odds cache exists to avoid re-buying metered odds after a container
    recycle -- which an EPHEMERAL file can never do. D1 is the store now, so it actually saves
    the calls it was written for."""
    import json  # noqa: PLC0415
    import time as _t  # noqa: PLC0415

    disk = tmp_path / "odds_cache.json"
    disk.write_text(json.dumps({"ts": _t.time(), "odds": {"Disk|Won": {"total": 41.5}}}),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "ODDS_CACHE_FILE", disk)

    d1_payload = {"ts": _t.time(), "odds": {"D1|Won": {"total": 52.5}}}
    monkeypatch.setattr(app_module.d1_write_path, "load_state",
                        lambda key: json.dumps(d1_payload) if key == "odds_cache" else None)
    monkeypatch.setattr(app_module, "ODDS_TTL", 86400)

    got = app_module._odds_disk_cache_get()
    assert ("D1", "Won") in got, "the ephemeral disk cache won over D1"
    assert ("Disk", "Won") not in got

    # Regard-of-age fallback reads D1 too.
    monkeypatch.setattr(app_module, "ODDS_TTL", 0)
    assert ("D1", "Won") in (app_module._odds_disk_cache_any_age() or {})


def test_odds_cache_writes_reach_the_d1_door(app_module, monkeypatch, tmp_path):
    """A write that only lands in the file is the bug: the next recycle would re-buy the odds."""
    import json  # noqa: PLC0415

    monkeypatch.setattr(app_module, "ODDS_CACHE_FILE", tmp_path / "odds_cache.json")
    saved: dict = {}
    monkeypatch.setattr(app_module.d1_write_path, "save_state",
                        lambda k, v: saved.__setitem__(k, json.loads(v)) or 1)

    app_module._odds_disk_cache_set({("Texas", "OU"): {"total": 55.0}})
    assert saved.get("odds_cache", {}).get("odds", {}).get("Texas|OU"), \
        "the odds cache never reached the D1 door"


def test_movement_log_is_d1_first_and_beats_a_disagreeing_disk(app_module, monkeypatch, tmp_path):
    """Phase 6: the rolling 7-day CLV record lived on an EPHEMERAL disk, so a recycle destroyed
    the audit trail of what the line actually did. D1 is the store now."""
    import json  # noqa: PLC0415
    import time as _t  # noqa: PLC0415

    disk = tmp_path / "line_movements.json"
    disk.write_text(json.dumps({"ts": _t.time(), "movements": [{"ts": _t.time(), "game": "Disk"}]}),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "LINE_MOVEMENTS_FILE", disk)
    monkeypatch.setattr(
        app_module.d1_write_path, "load_state",
        lambda k: json.dumps({"ts": _t.time(), "movements": [{"ts": _t.time(), "game": "D1"}]})
        if k == "line_movements" else None)

    got = app_module._load_movement_log()
    assert [m["game"] for m in got] == ["D1"], "the ephemeral disk log won over D1"


def test_movement_append_extends_the_d1_log_and_reaches_the_d1_door(app_module, monkeypatch, tmp_path):
    """An append that only lands in the file is the bug: the next recycle erases the history."""
    import json  # noqa: PLC0415
    import time as _t  # noqa: PLC0415

    monkeypatch.setattr(app_module, "LINE_MOVEMENTS_FILE", tmp_path / "line_movements.json")
    monkeypatch.setattr(
        app_module.d1_write_path, "load_state",
        lambda k: json.dumps({"ts": 0, "movements": [{"ts": _t.time(), "game": "existing"}]})
        if k == "line_movements" else None)
    saved: dict = {}
    monkeypatch.setattr(app_module.d1_write_path, "save_state",
                        lambda k, v: saved.__setitem__(k, json.loads(v)) or 1)

    app_module._append_movement_log([{"ts": _t.time(), "game": "new"}])
    got = [m["game"] for m in saved.get("line_movements", {}).get("movements", [])]
    assert got == ["existing", "new"], got


def test_movement_log_falls_back_to_disk_when_d1_is_empty(app_module, monkeypatch, tmp_path):
    import json  # noqa: PLC0415
    import time as _t  # noqa: PLC0415

    disk = tmp_path / "line_movements.json"
    disk.write_text(json.dumps({"ts": _t.time(), "movements": [{"ts": _t.time(), "game": "Disk"}]}),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "LINE_MOVEMENTS_FILE", disk)
    monkeypatch.setattr(app_module.d1_write_path, "load_state", lambda k: None)
    assert [m["game"] for m in app_module._load_movement_log()] == ["Disk"]


# ------------------------------------------------------- KNOWN-BROKEN (F1), xfail-strict


def test_d1_results_keep_up_with_the_served_week(served):
    """D1 `games` must hold finals for the most recently completed week the site is on.

    F1, fixed in Phase 2 (2026-09-28): the xfail was lifted only after BOTH halves existed
    -- the backfill that filled the gap AND `test_live_finals_fetch_archives_to_d1`, which
    proves the live path keeps it filled. Lifting it on the backfill alone would have left
    a green test guarding nothing.
    """
    d = _d1()
    rows = d.query(
        "SELECT MAX(week) w FROM games WHERE season=? AND home_score IS NOT NULL", [SEASON])
    d1_week = rows[0]["w"] if rows else None

    served_week = served["rankings"]["week"]          # e.g. "September 28, 2026"
    assert isinstance(served_week, str) and served_week, "cannot read the served week"

    # The site is on the current week; its most recent COMPLETED week is week-1.
    current = _current_week_estimate()
    assert d1_week is not None, "no scored games in D1 at all"
    assert d1_week >= current - 1, (
        f"D1 `games` only has finals through week {d1_week}, but the site is on week "
        f"{current} ({served_week}). Results since week {d1_week} are missing from D1 (F1).")


def _current_week_estimate():
    """The season week the site is serving, derived from D1's own schedule rows."""
    rows = _d1().query(
        "SELECT week, MIN(kickoff) k FROM games WHERE season=? AND kickoff >= ? "
        "GROUP BY week ORDER BY week ASC LIMIT 1", [SEASON, _today_iso()])
    return rows[0]["week"] if rows else 1


def _today_iso():
    from datetime import datetime, timezone  # noqa: PLC0415
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")