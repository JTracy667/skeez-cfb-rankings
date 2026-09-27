"""Phase 2 tests: the /api/matchup engine and its admin gate.

The polarity table is the risky part of this feature -- one wrong sign inverts a
whole row and nothing else would notice -- so the tests assert DIRECTION, not just
"a number came back". All fixtures are synthetic in-memory teams: no disk, no
network, no CFBD.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402

LIVE = Path(app.BASE_DIR) / "data" / "cfbd_analytics.json"


def _team(name, **kw):
    d = {"name": name, "mascot": kw.pop("mascot", ""), "conf": "TEST", "composite": 50.0}
    d.update(kw)
    return d


# A 4-team field with hand-set values, so every rank and every edge is computable
# by inspection.
FIELD = [
    _team("Alpha", mascot="Alphas", off_pass_success=0.50, def_pass_success=0.30,
          def_havoc_total=0.20, off_stuff_rate=0.10, net_field_pos=5.0),
    _team("Bravo", mascot="Bravos", off_pass_success=0.40, def_pass_success=0.40,
          def_havoc_total=0.10, off_stuff_rate=0.15, net_field_pos=2.0),
    _team("Charlie", mascot="Charlies", off_pass_success=0.30, def_pass_success=0.50,
          def_havoc_total=0.30, off_stuff_rate=0.20, net_field_pos=-1.0),
    _team("Delta", mascot="Deltas", off_pass_success=0.20, def_pass_success=0.20,
          def_havoc_total=0.05, off_stuff_rate=0.25, net_field_pos=-4.0),
]


class TestPolarityAndConfig(unittest.TestCase):
    def test_every_metric_key_exists_in_the_real_payload(self):
        """The engine may only reference fields the analytics record actually has."""
        if not LIVE.exists():
            self.skipTest("no live analytics file")
        live_keys = set(json.loads(LIVE.read_text(encoding="utf-8"))[0].keys())
        available = live_keys | set(app.ADV_MATCHUP_FIELDS) | set(app.ADV_MATCHUP_RESUME_FIELDS)
        missing = [k for k, _l, _s, _p, _d in app.MATCHUP_METRICS if k not in available]
        self.assertEqual([], missing, f"engine references fields that do not exist: {missing}")

    def test_sections_are_declared_and_all_used(self):
        declared = {k for k, _label in app.MATCHUP_SECTIONS}
        used = {s for _k, _l, s, _p, _d in app.MATCHUP_METRICS}
        self.assertEqual(declared, used)

    def test_polarity_is_only_plus_or_minus_one(self):
        for key, _label, _sec, pol, _dec in app.MATCHUP_METRICS:
            self.assertIn(pol, (+1, -1), key)

    def test_net_field_pos_is_positive_is_advantage(self):
        """Jeff-confirmed flip: def - off, so a positive value is the advantage."""
        row = dict((k, (None, None, None, p, d)) for k, _l, _s, p, d in app.MATCHUP_METRICS)
        self.assertEqual(row["net_field_pos"][3], +1)

    def test_rank_pool_is_fbs_not_the_whole_payload(self):
        """Ranks must be national ranks over FBS (~138), not over all ~685 records."""
        if not LIVE.exists():
            self.skipTest("no live analytics file")
        teams = json.loads(LIVE.read_text(encoding="utf-8"))
        self.assertGreater(len(teams), 400, "payload should include FCS teams")
        pool = app._matchup_rank_pool(teams)
        self.assertGreaterEqual(len(pool), 120)
        self.assertLessEqual(len(pool), 145)
        # and no produced rank may exceed the pool
        ctx = app.build_matchup_rankings(teams)
        for key, meta in ctx.items():
            if meta["n"]:
                self.assertLessEqual(max(meta["rank"].values()), len(pool), key)

    def test_pool_falls_back_rather_than_emptying_every_badge(self):
        """A matcher hiccup must degrade to 'all teams', never to zero ranks."""
        small = [_team("Alpha"), _team("Bravo")]
        self.assertEqual(len(app._matchup_rank_pool(small)), 2)


class TestRankDirection(unittest.TestCase):
    def setUp(self):
        self.ctx = app.build_matchup_rankings(FIELD)

    def test_higher_is_better_metric_ranks_descending(self):
        r = self.ctx["off_pass_success"]["rank"]
        self.assertEqual((r["Alpha"], r["Delta"]), (1, 4))

    def test_lower_is_better_metric_ranks_ascending(self):
        """def_pass_success is success rate ALLOWED -> the lowest value is rank 1."""
        r = self.ctx["def_pass_success"]["rank"]
        self.assertEqual(r["Delta"], 1)   # 0.20 allowed, best
        self.assertEqual(r["Charlie"], 4)  # 0.50 allowed, worst

    def test_higher_is_better_defensive_metric(self):
        r = self.ctx["def_havoc_total"]["rank"]
        self.assertEqual((r["Charlie"], r["Delta"]), (1, 4))

    def test_none_values_are_excluded_not_ranked_zero(self):
        teams = FIELD + [_team("Echo")]  # no metrics at all
        ctx = app.build_matchup_rankings(teams)
        self.assertNotIn("Echo", ctx["off_pass_success"]["rank"])
        self.assertEqual(ctx["off_pass_success"]["n"], 4)


class TestLeaderLogic(unittest.TestCase):
    def setUp(self):
        self.ctx = app.build_matchup_rankings(FIELD)

    def _row(self, home, away, key):
        out = app.matchup_breakdown(home, away, FIELD, self.ctx)
        for sec in out["sections"]:
            for r in sec["rows"]:
                if r["key"] == key:
                    return r
        raise AssertionError(key)

    def test_lower_is_better_defensive_row_gives_edge_to_the_stingier_defense(self):
        # home Alpha allows .30, away Bravo allows .40 -> Alpha (home) must lead.
        r = self._row("Alpha", "Bravo", "def_pass_success")
        self.assertEqual(r["leader"], "home")
        self.assertEqual(r["edge"], -0.1)

    def test_higher_is_better_row_gives_edge_to_the_bigger_value(self):
        # home Alpha .50 vs away Bravo .40 -> the larger value (home) leads.
        r = self._row("Alpha", "Bravo", "off_pass_success")
        self.assertEqual(r["leader"], "home")
        self.assertEqual(r["away"]["value"], 0.40)
        self.assertEqual(r["edge"], -0.1)

    def test_missing_metric_yields_no_leader_rather_than_a_wrong_one(self):
        teams = [_team("Alpha"), _team("Bravo", off_pass_success=0.4)]
        ctx = app.build_matchup_rankings(teams)
        out = app.matchup_breakdown("Alpha", "Bravo", teams, ctx)
        rows = [r for s in out["sections"] for r in s["rows"] if r["key"] == "off_pass_success"]
        self.assertIsNone(rows[0]["leader"])
        self.assertIsNone(rows[0]["edge"])


class TestBreakdownShape(unittest.TestCase):
    def test_breakdown_covers_every_metric_and_both_edge_directions(self):
        out = app.matchup_breakdown("Alpha", "Bravo", FIELD)
        rows = [r for s in out["sections"] for r in s["rows"]]
        self.assertEqual(len(rows), len(app.MATCHUP_METRICS))
        self.assertEqual(len(out["edges"]), len(app.MATCHUP_EDGES) * 2)
        dirs = {e["direction"] for e in out["edges"]}
        self.assertEqual(dirs, {"away_o_vs_home_d", "home_o_vs_away_d"})
        self.assertEqual(out["field_size"], 4)

    def test_section_win_tally_matches_rows(self):
        out = app.matchup_breakdown("Alpha", "Bravo", FIELD)
        for sec in out["sections"]:
            for side in ("home", "away"):
                expect = sum(1 for r in sec["rows"] if r["leader"] == side)
                self.assertEqual(sec["wins"][side], expect)

    def test_team_resolution_errors_are_reported_not_guessed(self):
        out = app.matchup_breakdown("Nobody", "Alpha", FIELD)
        self.assertIn("error", out)
        out = app.matchup_breakdown("Alpha", "Nobody", FIELD)
        self.assertIn("error", out)
        self.assertTrue(out["error"].startswith("away:"))


class TestTeamResolver(unittest.TestCase):
    def test_exact_mascot_and_unique_substring(self):
        teams = [_team("Oregon", mascot="Ducks"), _team("USC", mascot="Trojans")]
        self.assertEqual(app.resolve_team("Oregon", teams)[0]["name"], "Oregon")
        self.assertEqual(app.resolve_team("oregon", teams)[0]["name"], "Oregon")
        self.assertEqual(app.resolve_team("Ducks", teams)[0]["name"], "Oregon")
        self.assertEqual(app.resolve_team("us", teams)[0]["name"], "USC")  # case-insensitive

    def test_ambiguous_query_is_refused_with_the_candidates(self):
        teams = [_team("Miami (FL)"), _team("Miami (OH)")]
        rec, err = app.resolve_team("Miami", teams)
        self.assertIsNone(rec)
        self.assertIn("ambiguous", err)
        self.assertIn("Miami (FL)", err)

    def test_empty_query(self):
        rec, err = app.resolve_team("  ", [])
        self.assertIsNone(rec)
        self.assertTrue(err)


class TestEndpointIsGated(unittest.TestCase):
    """A gate that silently disappears is invisible in normal use -- assert it."""

    def _route(self):
        for route in app.app.routes:
            if getattr(route, "path", None) == "/api/matchup":
                return route
        return None

    def test_route_exists(self):
        self.assertIsNotNone(self._route(), "/api/matchup is not registered")

    def test_route_requires_the_admin_token(self):
        route = self._route()
        deps = getattr(getattr(route, "dependant", None), "dependencies", []) or []
        self.assertTrue(deps, "/api/matchup is NOT admin-gated")
        names = [getattr(d.call, "__name__", "") for d in deps]
        self.assertIn("require_admin", names)

    def test_gate_fails_closed_when_no_token_is_configured(self):
        """Same contract as the other ops routes: unset ADMIN_TOKEN -> 503, not open."""
        saved = app.ADMIN_TOKEN
        try:
            app.ADMIN_TOKEN = ""
            with self.assertRaises(Exception) as cm:
                app.require_admin(x_admin_token=None)
            self.assertIn("503", str(cm.exception))
        finally:
            app.ADMIN_TOKEN = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)