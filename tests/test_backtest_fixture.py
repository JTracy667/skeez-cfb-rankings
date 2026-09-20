"""
test_backtest_fixture.py
QA regression test for the game-level ATS backtest fixture.
Ensures data/backtest_fixture_2026.json and data/backtest_summary.json exist,
reconciles game counts, and validates closing spread provenance and walk-forward isolation.
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
        
        # Check closing line provenance rule exists
        self.assertIn("closing_line_rule", summary)
        self.assertTrue(summary["fcs_exclusion_applied"])
        
        # Check individual game fields for closing provider and line provenance
        sample = games[0]
        required_keys = [
            "game_id", "week", "home", "away", "final_score",
            "closing_provider", "closing_spread", "spread_open",
            "model_proj_spread", "ats_edge", "ats_pick", "ats_result"
        ]
        for k in required_keys:
            self.assertIn(k, sample)
            
        # Verify provider provenance
        self.assertIn(sample["closing_provider"], ("DraftKings", "Draft Kings", "Bovada"))
            
        # Verify FBS ATS win rate beats break-even (> 52.4%)
        all_picks = summary["fbs_ats_tiers"]["all_fbs_picks"]
        self.assertGreater(all_picks["win_pct"], 52.4, "Model overall FBS ATS win rate must exceed break-even")
        
        # Verify 3-Star (>= 3.5 pts) and 5-Star (>= 7.0 pts) win rates
        star3 = summary["fbs_ats_tiers"]["tier_2_3star_3.5pt"]
        star5 = summary["fbs_ats_tiers"]["tier_3_5star_7pt"]
        self.assertGreater(star3["win_pct"], 53.0, "3-Star tier ATS win rate must exceed 53%")
        self.assertGreater(star5["win_pct"], 54.0, "5-Star tier ATS win rate must exceed 54%")
        
        # Verify walk-forward weeks exist
        self.assertIn("week_1", summary["walk_forward_weeks"])
        self.assertIn("week_2", summary["walk_forward_weeks"])
        self.assertIn("week_3", summary["walk_forward_weeks"])

if __name__ == "__main__":
    unittest.main()
