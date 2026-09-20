"""
test_backtest_fixture.py
QA regression test for the game-level ATS backtest fixture.
Ensures data/backtest_fixture_2026.json and data/backtest_summary.json exist,
reconciles game counts, and validates closing spread logic.
"""

import json
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

class TestBacktestFixture(unittest.TestCase):
    def test_fixture_integrity(self):
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
        
        # Spot check individual game fields
        sample = games[0]
        required_keys = [
            "game_id", "week", "home", "away", "final_score",
            "closing_spread", "model_proj_spread", "ats_edge", "ats_pick", "ats_result"
        ]
        for k in required_keys:
            self.assertIn(k, sample)
            
        # Verify model delivers > 52.4% (break-even against -110 juice)
        all_picks = summary["ats_tiers"]["all_picks"]
        self.assertGreater(all_picks["win_pct"], 52.4, "Model overall ATS win rate must exceed break-even")
        
        # Verify high edge tier (>= 7.0 pts) delivers positive expectancy
        high_edge = summary["ats_tiers"]["high_edge_7pt"]
        self.assertGreater(high_edge["win_pct"], 53.0, "High edge ATS win rate must exceed 53%")

if __name__ == "__main__":
    unittest.main()
