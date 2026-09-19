"""
Test suite for CFB injury tracking and persistent QB replacement value model.
Validates Jeff Tracy rule (Star QB OUT = -10.0 pts) and multi-week projection shifts.
"""

import os
import sys
import unittest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app
import scripts.fetch_injuries as scraper


class TestCFBInjuries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app.app)

    def test_star_qb_ten_point_deduction(self):
        """Star QB out must receive the full -10.0 point deduction (Jeff Tracy rule)."""
        star_team = {"conf": "SEC", "composite": 88.0, "sp_plus": 25.0}
        pts, tier = scraper.calculate_player_deduction("Texas", "QB", "Out - Ankle", star_team)
        self.assertEqual(pts, -10.0, "Star QB OUT must be exactly -10.0 points")
        self.assertEqual(tier, "star_qb")

    def test_star_qb_questionable_half_deduction(self):
        """Star QB Questionable must receive 50% deduction (-5.0 pts)."""
        star_team = {"conf": "SEC", "composite": 88.0, "sp_plus": 25.0}
        pts, tier = scraper.calculate_player_deduction("Georgia", "QB", "Questionable - Hamstring", star_team)
        self.assertEqual(pts, -5.0)

    def test_p4_and_g5_starter_tiers(self):
        """P4 starter QB receives -7.0, G5 receives -4.5."""
        p4_team = {"conf": "Big 12", "composite": 65.0, "sp_plus": 8.0}
        pts_p4, tier_p4 = scraper.calculate_player_deduction("Baylor", "QB", "Out - Knee", p4_team)
        self.assertEqual(pts_p4, -7.0)
        self.assertEqual(tier_p4, "p4_starter_qb")

        g5_team = {"conf": "MAC", "composite": 40.0, "sp_plus": -10.0}
        pts_g5, tier_g5 = scraper.calculate_player_deduction("Buffalo", "QB", "Out - Shoulder", g5_team)
        self.assertEqual(pts_g5, -4.5)
        self.assertEqual(tier_g5, "g5_starter_qb")

    def test_h2h_projection_star_qb_shift(self):
        """Projected head-to-head margin must shift by exactly 10.0 pts when star QB is out."""
        home_team = {"name": "Texas", "composite": 85.0, "sp_plus": 25.0, "classification": "FBS"}
        away_team = {"name": "Oklahoma", "composite": 80.0, "sp_plus": 20.0, "classification": "FBS"}

        healthy_h2h = app.project_head_to_head(home_team, away_team, neutral_site=True)
        injured_h2h = app.project_head_to_head(
            home_team, away_team, neutral_site=True, home_injury_adj=-10.0, away_injury_adj=0.0
        )

        margin_diff = healthy_h2h["differential"] - injured_h2h["differential"]
        self.assertAlmostEqual(margin_diff, 10.0, delta=0.5,
            msg="Game differential must shift by 10 points when home Star QB is out")
        self.assertLess(injured_h2h["total"], healthy_h2h["total"],
            msg="Game total must decrease when star QB is missing")

    def test_injuries_api_endpoints(self):
        """Verify /api/injuries and /api/injuries/override endpoints."""
        r_get = self.client.get("/api/injuries")
        self.assertEqual(r_get.status_code, 200)
        data = r_get.json()
        self.assertIn("teams", data)

        # Test manual override endpoint
        override_payload = {
            "team": "TestState",
            "player": "Star Quarterback",
            "pos": "QB",
            "status": "Out",
            "deduction": -10.0
        }
        r_post = self.client.post("/api/injuries/override", json=override_payload)
        self.assertEqual(r_post.status_code, 200)
        res = r_post.json()
        self.assertEqual(res["status"], "updated")
        self.assertEqual(res["net_injury_points"], -10.0)

        # Clear override
        r_clear = self.client.post("/api/injuries/override", json={"team": "TestState", "action": "clear"})
        self.assertEqual(r_clear.status_code, 200)


if __name__ == "__main__":
    unittest.main()
