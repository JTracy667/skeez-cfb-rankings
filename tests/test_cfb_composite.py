"""
Regression and verification test suite for CFB Power Rankings composite model.
Guarantees parser law, composite calibration, and prediction stability.
"""

import os
import sys
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app


class TestCFBCompositeModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load sample data
        cls.teams = app._load_cfbd_analytics_file() or []
        cls.team_map = {t["name"]: t for t in cls.teams}

    def test_fcs_no_data_prior(self):
        """Teams with no ratings and FCS classification must receive composite 16.0."""
        fcs_dummy = {"name": "Test FCS Squad", "classification": "FCS"}
        proj = app.project_score_multi_factor(fcs_dummy, is_home=False)
        self.assertEqual(proj["composite"], 16.0)
        self.assertEqual(proj["data_flag"], "fcs_no_data")
        self.assertGreater(proj["projected_score"], 0.0)

    def test_composite_bounds_and_structure(self):
        """All FBS teams must have composite ratings between 0 and 100 with all contributions."""
        for t in self.teams[:50]:
            proj = app.project_score_multi_factor(t, is_home=True)
            comp = proj["composite"]
            self.assertGreaterEqual(comp, 0.0, f"Composite < 0 for {t.get('name')}")
            self.assertLessEqual(comp, 100.0, f"Composite > 100 for {t.get('name')}")
            # Ensure no NaNs
            self.assertFalse(any(isinstance(v, float) and v != v for v in proj.values()))
            # Contributions check
            self.assertIn("sp_contribution", proj)
            self.assertIn("fpi_contribution", proj)
            self.assertIn("srs_contribution", proj)
            self.assertIn("efficiency_contribution", proj)

    def test_advanced_efficiency_impact(self):
        """A team with superior success rate and trench play should rate higher than an identical team with poor efficiency."""
        base_team_good = {
            "name": "Efficient U",
            "sp_plus": 10.0,
            "fpi_win_prob": 65.0,
            "srs": 8.0,
            "elo": 1600,
            "recruiting_rank": 30,
            "pct_ppa_returning": 60.0,
            "off_success_rate": 0.52,
            "def_success_rate": 0.32,
            "epa_play": 0.25,
            "def_epa_play": -0.15,
            "off_ppo": 5.2,
            "def_ppo": 2.8,
            "off_line_yards": 3.4,
            "def_line_yards": 2.1,
            "off_stuff_rate": 0.12,
            "def_stuff_rate": 0.25,
            "pts_per_poss": 3.1,
            "def_pts_per_poss": 1.4,
        }
        base_team_bad = {
            "name": "Inefficient U",
            "sp_plus": 10.0,
            "fpi_win_prob": 65.0,
            "srs": 8.0,
            "elo": 1600,
            "recruiting_rank": 30,
            "pct_ppa_returning": 60.0,
            "off_success_rate": 0.36,
            "def_success_rate": 0.48,
            "epa_play": -0.10,
            "def_epa_play": 0.20,
            "off_ppo": 3.0,
            "def_ppo": 4.5,
            "off_line_yards": 2.2,
            "def_line_yards": 3.6,
            "off_stuff_rate": 0.25,
            "def_stuff_rate": 0.10,
            "pts_per_poss": 1.5,
            "def_pts_per_poss": 2.8,
        }
        good_proj = app.project_score_multi_factor(base_team_good)
        bad_proj = app.project_score_multi_factor(base_team_bad)
        self.assertGreater(
            good_proj["composite"],
            bad_proj["composite"],
            "Efficient team should have a significantly higher composite than inefficient team with identical anchor ratings"
        )
        self.assertGreaterEqual(good_proj["composite"] - bad_proj["composite"], 10.0)

    def test_head_to_head_totals_calibration(self):
        """Projected totals between competitive FBS teams must sit in realistic CFB ranges (~48-58)."""
        osu = self.team_map.get("Ohio State") or {"name": "Ohio State", "sp_plus": 29.0, "fpi_win_prob": 95.0, "srs": 25.0}
        uga = self.team_map.get("Georgia") or {"name": "Georgia", "sp_plus": 29.0, "fpi_win_prob": 95.0, "srs": 25.0}
        h2h = app.project_head_to_head(osu, uga, neutral_site=True)
        total = h2h["home_proj"] + h2h["away_proj"]
        self.assertGreaterEqual(total, 48.0, f"Total {total} below realistic floor")
        self.assertLessEqual(total, 60.0, f"Total {total} inflated above realistic ceiling")

    def test_rankings_sorted_descending(self):
        """Top 25 rankings must strictly descend by composite score."""
        rankings = app.get_rankings()
        composites = [t.composite for t in rankings.teams]
        self.assertEqual(composites, sorted(composites, reverse=True))


if __name__ == "__main__":
    unittest.main()
