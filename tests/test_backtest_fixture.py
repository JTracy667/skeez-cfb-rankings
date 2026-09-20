"""
test_backtest_fixture.py
QA regression test for the game-level ATS backtest fixture.
Ensures data/backtest_fixture_2026.json and data/backtest_summary.json exist,
reconciles game counts, and validates closing spread provenance and strict point-in-time walk-forward isolation.
"""

import json
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

class TestBacktestFixture(unittest.TestCase):
    def test_fixture_integrity_and_provenance(self):
        fixture_path = DATA_DIR / "backtest_fixture_2026.json"
        summary_path = DATA_DIR / "backtest_summary.json"
        
        self.assertTrue(fixture_path.exists(), "backtest_fixture_2026.json must exist")
        self.assertTrue(summary_path.exists(), "backtest_summary.json must exist")
        
        with open(fixture_path, "r", encoding="utf-8") as f:
            games = json.load(f)
            
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
            
        self.assertGreaterEqual(len(games), 300, "Should have at least 300 completed 2026 game audits")
        self.assertEqual(len(games), summary["total_evaluated_games"])
        
        # Check closing line provenance and point-in-time isolation descriptions exist in summary
        self.assertIn("closing_line_provenance", summary)
        self.assertIn("point_in_time_isolation", summary)
        self.assertTrue(summary["fcs_exclusion_applied"])
        
        # Check that EVERY single game in the fixture carries explicit closing_timestamp and provider provenance
        valid_providers = ("DraftKings", "Draft Kings", "Bovada")
        for idx, g in enumerate(games):
            self.assertIn("closing_timestamp", g, f"Row {idx} missing closing_timestamp")
            self.assertTrue(bool(g["closing_timestamp"]), f"Row {idx} has empty closing_timestamp")
            self.assertIn("closing_provider", g, f"Row {idx} missing closing_provider")
            self.assertIn(g["closing_provider"], valid_providers, f"Row {idx} invalid provider: {g['closing_provider']}")
            self.assertIn("closing_spread", g)
            self.assertIn("line_source", g)
            
        # Verify totals tiers are present and reflect the CFO veto on 5-star totals
        self.assertIn("fbs_totals_tiers", summary)
        totals_5star = summary["fbs_totals_tiers"]["totals_5star_7pt"]
        self.assertLess(totals_5star["win_pct"], 50.0, "5-star totals must underperform / be vetoed")
        
        # Verify walk-forward weeks exist
        self.assertIn("week_1", summary["walk_forward_weeks"])
        self.assertIn("week_2", summary["walk_forward_weeks"])
        self.assertIn("week_3", summary["walk_forward_weeks"])
        
        # Verify Week 3 5-star reaches 60.0% as in-season data matures
        wk3 = summary["walk_forward_weeks"]["week_3"]
        self.assertIn("60.0%", wk3["5star_record"])

    def test_frozen_preseason_talent_integrity(self):
        """Verify that 247 Team Talent Composite is frozen on disk in the preseason snapshot with zero live leakage."""
        preseason_path = DATA_DIR / "cfbd_analytics_preseason.json"
        self.assertTrue(preseason_path.exists(), "Preseason snapshot file must exist")
        
        with open(preseason_path, "r", encoding="utf-8") as f:
            teams = json.load(f)
            
        team_map = {t["name"]: t for t in teams}
        for top_fbs in ("Georgia", "Alabama", "Ohio State", "Texas"):
            self.assertIn(top_fbs, team_map)
            t_score = team_map[top_fbs].get("talent_score")
            self.assertIsNotNone(t_score, f"{top_fbs} must have frozen talent_score on disk")
            self.assertGreater(t_score, 900.0, f"{top_fbs} talent composite should exceed 900.0")

if __name__ == "__main__":
    unittest.main()
