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


def _d1_metric_map_at(keys, week, stamp):
    """{(team_name, stat_key): value} for ONE published pull (its exact stamp).

    Serving selects by the publication record's stamp, so parity must too -- comparing
    against `MAX(recorded_at)` would use the very selector this work order removed, and a
    later single-row poll would make both sides agree on the wrong snapshot.
    """
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = _d1().query(
        f"SELECT t.name n, o.stat_key k, o.value v FROM stat_observations o "
        f"JOIN teams t ON t.team_id=o.subject_id "
        f"WHERE o.season=? AND o.week=? AND o.subject_type='team' AND o.recorded_at=? "
        f"AND o.stat_key IN ({marks})",
        [SEASON, week, stamp, *keys])
    return {(r["n"], r["k"]): r["v"] for r in rows}

# --------------------------------------------------------------------------- STRICT


def test_served_analytics_equals_the_published_complete_pull(served):
    """Serving must equal the EXACT snapshot the publication record names (Task 4).

    The previous version compared against `MAX(recorded_at)` across all rows for the week --
    the SAME selector the serving path used, so it could never observe the defect (a later
    single-row poll displacing the whole pull). Both sides now select one snapshot: the
    marker's stamp.

    With no publication record, serving is on the documented disk fallback; the fallback
    contract is then verified explicitly and the run is labelled TRANSITION PENDING rather
    than being reported as a D1 parity pass.
    """
    import json  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    import d1_write_path  # noqa: PLC0415
    pub = d1_write_path.analytics_publication(SEASON)
    served_teams = served["analytics"]["teams"]

    if not pub:
        disk = json.loads((pathlib.Path(__file__).resolve().parents[1] / "data"
                           / "cfbd_analytics.json").read_text(encoding="utf-8"))
        disk_names = {t.get("name") for t in disk if t.get("name")}
        served_names = {t.get("name") for t in served_teams}
        assert len(served_names) >= MIN_TEAMS, "served universe suspiciously small"
        assert disk_names <= served_names, (
            "no publication record -> serving must be the documented disk fallback, but "
            f"{len(disk_names - served_names)} disk team(s) are not served")
        print("[parity] TRANSITION PENDING: no analytics publication record in D1; verified "
              "the disk fallback contract only. A complete pull publishes the marker.")
        return

    week, stamp = int(pub["week"]), str(pub["stamp"])
    stored_map = _d1_metric_map_at(("sp_plus", "srs"), week, stamp)
    checked, bad = 0, []
    # NB: must be the ANALYTICS payload -- /api/rankings is served from a D1 slate board,
    # so it cannot observe the analytics source at all (learned the hard way 2026-09-28).
    for t in served_teams[:40]:
        for key in ("sp_plus", "srs"):
            if t.get(key) is None:
                continue
            stored = stored_map.get((t["name"], key))
            if stored is None:
                continue
            checked += 1
            if abs(float(t[key]) - float(stored)) > TOL:
                bad.append(f"{t['name']} {key}: served {t[key]} != published {stored} "
                           f"(wk {week}, stamp {stamp})")

    assert checked >= MIN_TEAMS, (
        f"only {checked} (team, metric) pairs were comparable - parity is not being "
        "verified. Check the D1 join and the served payload shape, do not loosen this.")
    assert not bad, "served != published snapshot for:\n" + "\n".join(f"  - {b}" for b in bad)


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


def test_served_universe_and_d1_teams_agree_both_ways(served):
    """F3, CLOSED 2026-09-28 -- and closed in BOTH directions, because only one of them is
    satisfied by construction.

    (a) D1 knows a team the site cannot render -> the site is behind D1 (the original finding:
        Anna Maria College and Defiance College sat in `teams` unrenderable).
    (b) The site renders a team D1 cannot identify -> the archive cannot file that team's
        metrics at all. THIS is the dangerous direction (v49/v50-class) and it is NOT fixed by
        building the served list from D1, so the pair is not a tautology.

    Names are compared through the app's OWN matcher. CFBD's ratings endpoints use aliases the
    `/teams` `school` field does not carry -- cfbd_shared.team_aliases() names these three
    cases verbatim: 'Albany' vs 'UAlbany', 'Southeastern Louisiana' vs 'SE Louisiana', 'UTRGV'
    vs 'UT Rio Grande Valley'. Comparing raw `name` columns reports them as drift when the
    system resolves them perfectly, so a raw comparison here would cry wolf forever.
    """
    import cfbd_shared  # noqa: PLC0415

    rows = _d1().query("SELECT team_id, name FROM teams")
    d1_names = {r["name"] for r in rows}
    id_to_name = {r["team_id"]: r["name"] for r in rows}
    served_names = {t["name"] for t in served["analytics"]["teams"]}

    assert len(served_names) >= MIN_TEAMS, f"served universe suspiciously small: {len(served_names)}"

    # (a) every team D1 knows must be servable
    missing = sorted(d1_names - served_names)
    assert not missing, (
        f"{len(missing)} team(s) exist in D1 `teams` but cannot be served:\n"
        + "\n".join(f"  - {n}" for n in missing[:15])
    )

    # (b) every served team must resolve to a team D1 knows (alias-aware)
    aliases = cfbd_shared.team_aliases(SEASON) or {}

    def canon(name: str) -> str:
        return id_to_name.get(aliases.get(name)) or name

    unresolved = sorted(n for n in served_names if canon(n) not in d1_names
                        and canon(n) not in served_names)
    unresolvable = sorted(n for n in served_names if canon(n) not in d1_names)
    assert not unresolvable, (
        f"{len(unresolvable)} team(s) are SERVED but D1 cannot identify them, so their metrics\n"
        "can never be archived:\n"
        + "\n".join(f"  - {n}" for n in unresolvable[:15])
        + ("\n(unresolved after alias normalisation: " + ", ".join(unresolved[:10]) + ")"
           if unresolved else "")
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


def test_best_bets_is_d1_first_and_beats_a_disagreeing_disk(app_module, monkeypatch, tmp_path):
    """Phase 6: the tracked best-bets record (locked picks + graded results) was a
    read-modify-write on an EPHEMERAL file, so a recycle reset it -- v49/v50 class, on the
    tracker."""
    import json  # noqa: PLC0415

    disk = tmp_path / "best_bets.json"
    disk.write_text(json.dumps({"season": 2026, "picks": [{"id": "disk"}], "results": []}),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "BEST_BETS_FILE", disk)
    monkeypatch.setattr(
        app_module.d1_write_path, "load_state",
        lambda k: json.dumps({"season": 2026, "picks": [{"id": "d1"}], "results": [{"id": "r1"}]})
        if k == "best_bets" else None)

    got = app_module._load_best_bets()
    assert [p["id"] for p in got["picks"]] == ["d1"], "the ephemeral disk tracker won over D1"
    assert len(got["results"]) == 1, "graded results were lost"


def test_best_bets_writes_reach_the_d1_door(app_module, monkeypatch, tmp_path):
    """A lock (or a graded result) that only lands in the file is the bug: the next recycle
    erases it."""
    import json  # noqa: PLC0415

    monkeypatch.setattr(app_module, "BEST_BETS_FILE", tmp_path / "best_bets.json")
    saved: dict = {}
    monkeypatch.setattr(app_module.d1_write_path, "save_state",
                        lambda k, v: saved.__setitem__(k, json.loads(v)) or 1)

    app_module._save_best_bets({"season": 2026, "picks": [{"id": "p1"}], "results": []})
    assert saved.get("best_bets", {}).get("picks", [{}])[0].get("id") == "p1", \
        "the best-bets write never reached the D1 door"


def test_best_bets_falls_back_to_disk_when_d1_is_empty(app_module, monkeypatch, tmp_path):
    import json  # noqa: PLC0415

    disk = tmp_path / "best_bets.json"
    disk.write_text(json.dumps({"season": 2026, "picks": [{"id": "disk"}], "results": []}),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "BEST_BETS_FILE", disk)
    monkeypatch.setattr(app_module.d1_write_path, "load_state", lambda k: None)
    assert app_module._load_best_bets()["picks"][0]["id"] == "disk"


def test_analytics_identity_is_d1_first_and_beats_a_disagreeing_disk(app_module, monkeypatch, tmp_path):
    """Phase 6: the identity/string fields (conf, streak) came from the image-copied file, so a
    recycle reverted them to BUILD-TIME values. `conf` and `streak` are not static."""
    import json  # noqa: PLC0415

    disk = tmp_path / "cfbd_analytics.json"
    disk.write_text(json.dumps([{"name": "Texas", "conf": "DISK", "streak": "W99"}]),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "_CFBD_ANALYTICS_FILE", disk)
    monkeypatch.setattr(
        app_module.d1_write_path, "load_state",
        lambda k: json.dumps({"Texas": {"name": "Texas", "conf": "SEC", "streak": "W3"}})
        if k == "analytics_identity" else None)
    monkeypatch.setattr(app_module.d1_write_path, "load_team_analytics",
                        lambda *a, **k: [{"name": "Texas", "sp_plus": 12.5}])

    out = app_module._served_analytics()
    tx = next(t for t in out if t["name"] == "Texas")
    assert tx["conf"] == "SEC", "the ephemeral disk identity won over D1"
    assert tx["streak"] == "W3", "streak reverted to the build-time value"
    assert tx["sp_plus"] == 12.5, "D1 numerics must still overlay"


def test_analytics_identity_writes_reach_the_d1_door(app_module, monkeypatch):
    """A pull that only writes identity to the ephemeral file is the bug."""
    import json  # noqa: PLC0415

    saved: dict = {}
    monkeypatch.setattr(app_module.d1_write_path, "save_state",
                        lambda k, v: saved.__setitem__(k, json.loads(v)) or 1)
    n = app_module._store_analytics_identity(
        [{"name": "Texas", "conf": "SEC", "streak": "W3", "sp_plus": 12.5}])
    assert n == 1
    got = saved.get("analytics_identity", {}).get("Texas", {})
    assert got.get("conf") == "SEC" and got.get("streak") == "W3"
    assert "sp_plus" not in got, "only STRING fields belong in the identity map"


def test_analytics_identity_falls_back_to_disk_when_d1_is_empty(app_module, monkeypatch, tmp_path):
    import json  # noqa: PLC0415

    disk = tmp_path / "cfbd_analytics.json"
    disk.write_text(json.dumps([{"name": "Texas", "conf": "DISK", "streak": "W99"}]),
                    encoding="utf-8")
    monkeypatch.setattr(app_module, "_CFBD_ANALYTICS_FILE", disk)
    monkeypatch.setattr(app_module.d1_write_path, "load_state", lambda k: None)
    monkeypatch.setattr(app_module.d1_write_path, "load_team_analytics", lambda *a, **k: [])

    out = app_module._served_analytics()
    assert next(t for t in out if t["name"] == "Texas")["conf"] == "DISK"


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
    if d1_week is None:
        pytest.skip(
            f"the D1 target holds no scored {SEASON} games, so the served week has nothing to be "
            "compared against. This is a PRECONDITION skip, not a pass -- run the gate against the "
            "populated database to actually exercise it.")
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