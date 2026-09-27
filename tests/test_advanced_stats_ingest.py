"""Phase 1 ingest tests for docs/ADVANCED_STATS_IMPLEMENTATION_PLAN.md.

HERMETIC BY DESIGN: no network access. Every "CFBD call" is served from the RAW
provider fixtures in data/backtest_cache/, which are real recorded responses. That
matters twice over:

  * it can never spend CFBD quota (the standing rule: tests must never hit a live
    metered fetcher -- a test boot spends the same cap the site does), and
  * it pins the ACTUAL provider key names. The plan's spec was written from the
    cards, not the payload; the payload is what decides, and it contains CFBD's
    own `totalOpportunies` typo. A test that mocks a hand-written dict would pass
    while production silently read None for all 138 teams.

The last test asserts the plan's headline claim -- ZERO additional quota cost --
by counting the CFBD endpoints fetch_live_analytics() actually calls.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402

CACHE = Path(app.BASE_DIR) / "data" / "backtest_cache"

# endpoint -> fixture filename template. Extend only when a fixture exists.
FIXTURES = {
    "stats/season/advanced": "raw_stats_season_advanced_year{year}.json",
    "ratings/fpi": "raw_ratings_fpi_year{year}.json",
    "ratings/sp": "raw_ratings_sp_year{year}.json",
}


def _load(fixture_tmpl: str, years) -> list:
    for y in years:
        p = CACHE / fixture_tmpl.format(year=y)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return []


class _FixtureCFBD:
    """Stand-in for app._cfbd_get that serves raw fixtures off disk."""

    def __init__(self):
        self.calls = []

    def __call__(self, endpoint, year=app.CFBD_YEAR, **extra):
        self.calls.append(endpoint)
        tmpl = FIXTURES.get(endpoint)
        if not tmpl:
            return []
        # Emulate the real current-season -> previous-season fallback ladder.
        return _load(tmpl, [year, app.CFBD_YEAR_FALLBACK, 2025])


def _advanced(year: int = 2025) -> dict:
    return {r["team"]: r for r in _load(FIXTURES["stats/season/advanced"], [year])}


class TestAdvancedIngestFields(unittest.TestCase):
    def test_field_list_is_exactly_what_the_parser_returns(self):
        """ADV_MATCHUP_FIELDS must not drift from _advanced_matchup_fields()."""
        produced = app._advanced_matchup_fields({}, {})
        self.assertEqual(set(produced), set(app.ADV_MATCHUP_FIELDS))

    def test_missing_subdicts_yield_none_without_raising(self):
        """Empty/partial records must not TypeError on a missing nested dict."""
        produced = app._advanced_matchup_fields({}, {})
        self.assertTrue(all(v is None for v in produced.values()), produced)
        # Sub-dicts present but explicitly null (CFBD does send nulls).
        produced = app._advanced_matchup_fields(
            {"havoc": None, "drives": None, "totalOpportunies": None, "fieldPosition": None},
            {"havoc": None, "drives": None, "totalOpportunies": None, "fieldPosition": None},
        )
        self.assertTrue(all(v is None for v in produced.values()), produced)

    def test_zero_drives_does_not_divide_by_zero(self):
        """Eckel rate needs a drives == 0 guard (plan Phase 1 checklist)."""
        produced = app._advanced_matchup_fields(
            {"drives": 0, "totalOpportunies": 5}, {"drives": 0, "totalOpportunies": 3})
        self.assertIsNone(produced["off_eckel_rate"])
        self.assertIsNone(produced["def_eckel_rate"])
        self.assertIsNone(produced["eckel_ratio"])

    def test_eckel_math_matches_definition(self):
        produced = app._advanced_matchup_fields(
            {"drives": 100, "totalOpportunies": 60},
            {"drives": 100, "totalOpportunies": 40})
        self.assertEqual(produced["off_eckel_rate"], 0.6)
        self.assertEqual(produced["def_eckel_rate"], 0.4)
        self.assertEqual(produced["eckel_ratio"], 0.6)

    def test_real_payload_extraction(self):
        """Spot-check against the real 2025 payload (Air Force, verified by hand)."""
        af = _advanced()["Air Force"]["offense"]
        self.assertEqual(af["havoc"]["total"], 0.09)          # fixture sanity
        produced = app._advanced_matchup_fields(
            _advanced()["Air Force"]["offense"], _advanced()["Air Force"]["defense"])
        self.assertEqual(produced["off_havoc_total"], 0.09)
        self.assertEqual(produced["off_havoc_front_seven"], 0.066)
        self.assertEqual(produced["off_havoc_db"], 0.024)
        self.assertEqual(produced["def_havoc_total"], 0.107)
        self.assertEqual(produced["off_drives"], 117)
        self.assertEqual(produced["off_total_opportunities"], 72)
        self.assertEqual(produced["off_eckel_rate"], round(72 / 117, 4))
        self.assertEqual(produced["def_drives"], 118)
        self.assertEqual(produced["def_total_opportunities"], 69)
        self.assertEqual(produced["off_rush_success"], round(0.48205928237129486, 4))
        self.assertEqual(produced["off_pass_explosiveness"], round(2.1339353422617204, 4))
        self.assertEqual(produced["off_standard_down_success"], round(0.5248, 4))
        self.assertEqual(produced["off_passing_down_success"], round(0.32323232323232326, 4))
        self.assertEqual(produced["off_field_pos_avg"], 71.8)
        self.assertEqual(produced["def_field_pos_avg"], 71.5)
        # def - off: positive = this team has the field-position advantage
        # (averageStart is distance-to-goal, so a HIGHER defensive value is better).
        self.assertEqual(produced["net_field_pos"], -0.3)

    def test_every_team_in_the_real_payload_is_fully_populated(self):
        """No team may end up with a missing value when CFBD sent one."""
        payload = _advanced()
        self.assertGreater(len(payload), 100)
        for team, rec in payload.items():
            produced = app._advanced_matchup_fields(rec.get("offense") or {},
                                                    rec.get("defense") or {})
            missing = [k for k, v in produced.items() if v is None]
            self.assertEqual([], missing, f"{team} missing {missing}")


class TestResumeAndSpecialTeamsFields(unittest.TestCase):
    def setUp(self):
        self.fake = _FixtureCFBD()
        self._orig = app._cfbd_get
        app._cfbd_get = self.fake

    def tearDown(self):
        app._cfbd_get = self._orig

    def test_fpi_resume_ranks_extracted_from_real_payload(self):
        fpi = app._cfbd_fpi()
        self.assertGreater(len(fpi), 100)
        populated = [t for t in fpi.values() if t.get("fpi_sor") is not None]
        self.assertGreater(len(populated), 100)
        for t in populated:
            self.assertIsInstance(t["fpi_sor"], int)
            self.assertIsInstance(t["fpi_sos"], int)
            self.assertIsInstance(t["fpi_game_control"], int)
        self.assertTrue(any(t.get("fpi_eff_special_teams") is not None for t in fpi.values()))

    def test_fpi_missing_resumeranks_does_not_raise(self):
        """A record with no resumeRanks/efficiencies must not blow up the fetch."""
        app._cfbd_get = lambda endpoint, year=app.CFBD_YEAR, **kw: (
            [{"team": "No Resume U", "fpi": 1.0}] if endpoint == "ratings/fpi" else [])
        fpi = app._cfbd_fpi()
        self.assertIsNone(fpi["No Resume U"]["fpi_sor"])
        self.assertIsNone(fpi["No Resume U"]["fpi_eff_special_teams"])

    def test_sp_special_teams_extracted_from_real_payload(self):
        sp = app._cfbd_sp()
        self.assertGreater(len(sp), 100)
        vals = [t["sp_special_teams"] for t in sp.values() if t.get("sp_special_teams") is not None]
        self.assertGreater(len(vals), 100)
        self.assertIn(1.5, vals)


class TestAnalyticsRecordAndQuotaClaim(unittest.TestCase):
    """The plan's headline claim: these metrics cost ZERO additional CFBD calls."""

    # Endpoints fetch_live_analytics() is allowed to touch. A new call for the
    # advanced metrics (e.g. /stats/season/advanced/... variants) fails this test.
    ALLOWED_ENDPOINTS = {"stats/season/advanced", "ratings/fpi", "ratings/sp"}

    def setUp(self):
        self.fake = _FixtureCFBD()
        self._orig_get = app._cfbd_get
        app._cfbd_get = self.fake

        # Stub the fetchers that are NOT under test so no other network path runs.
        self._stubs = {
            "_cfbd_teams": lambda: {t: {"nickname": "", "conference": "MWC"}
                                    for t in _advanced()},
            "_cfbd_recruiting": lambda: {},
            "_cfbd_talent": lambda: {},
            "_cfbd_srs": lambda: {},
            "_cfbd_elo": lambda: {},
            "_cfbd_season_stats": lambda: {},
            "_cfbd_ppa": lambda: {},
            "_cfbd_drives_for_teams": lambda names: {},
            "_cfbd_returning": lambda: {},
            "_cfbd_roster_experience": lambda: {},
            "_cfbd_records": lambda year=app.CFBD_YEAR: {},
        }
        self._saved = {}
        for name, fn in self._stubs.items():
            self._saved[name] = getattr(app, name)
            setattr(app, name, fn)

    def tearDown(self):
        app._cfbd_get = self._orig_get
        for name, fn in self._saved.items():
            setattr(app, name, fn)

    def test_no_new_cfbd_endpoint_is_introduced(self):
        app.fetch_live_analytics()
        self.assertEqual(self.ALLOWED_ENDPOINTS, set(self.fake.calls), self.fake.calls)
        # exactly one call each -- no duplicate fetch for the new metrics
        for endpoint in self.fake.calls:
            self.assertEqual(1, self.fake.calls.count(endpoint), endpoint)

    def test_analytics_record_carries_every_new_field(self):
        teams = app.fetch_live_analytics()
        self.assertTrue(teams)
        rec = {t["name"]: t for t in teams}["Air Force"]
        for field in app.ADV_MATCHUP_FIELDS:
            self.assertIn(field, rec, field)
        for field in ("fpi_sor", "fpi_sos", "fpi_game_control",
                      "fpi_eff_special_teams", "sp_special_teams"):
            self.assertIn(field, rec, field)
        # values actually arrived (not just present-as-None)
        self.assertEqual(rec["off_havoc_total"], 0.09)
        self.assertEqual(rec["off_drives"], 117)
        self.assertIsNotNone(rec["sp_special_teams"])
        self.assertIsNotNone(rec["fpi_sor"])

    def test_record_is_json_serializable(self):
        """The record is written to data/cfbd_analytics.json -- it must round-trip."""
        teams = app.fetch_live_analytics()
        blob = json.dumps(teams)
        self.assertGreater(len(blob), 1000)
        self.assertEqual(len(teams), len(json.loads(blob)))

    def test_existing_consumers_still_see_their_fields(self):
        """Additive change: nothing the composite/UI already reads may disappear."""
        rec = {t["name"]: t for t in app.fetch_live_analytics()}["Air Force"]
        for field in ("sp_plus", "fpi", "srs", "elo", "talent_score",
                      "off_success_rate", "def_success_rate", "off_ppo", "def_ppo",
                      "off_line_yards", "def_line_yards", "off_stuff_rate", "def_stuff_rate",
                      "off_power_success", "def_power_success",
                      "off_explosiveness", "def_explosiveness"):
            self.assertIn(field, rec, field)


if __name__ == "__main__":
    unittest.main(verbosity=2)