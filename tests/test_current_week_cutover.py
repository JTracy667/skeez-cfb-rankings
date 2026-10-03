"""The schedule page's default week: an UPCOMING-slate rule with a HARD FLOOR.

Regression (Jeff, 2026-10-03): the Schedule page defaulted to week 6 on Fri Oct 2
while week 5's entire Saturday slate -- including the week's marquee game -- was
still unplayed. Week 6 opens on a TUESDAY (Oct 6), so "4 days before the week's
first kickoff" reached back into the weekend it was supposed to follow.

The rule is now: cutover = 4 days before the week's first kickoff, floored to
midnight ET, but NEVER earlier than the midnight after the previous week's last
game. A standard Thu-Sat week is unaffected; midweek openers are corrected.

These tests drive the pure cutover function with synthetic schedules, so they
pin the rule itself rather than today's calendar.
"""
import os
import sys
import unittest
from datetime import timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402


def _g(week, iso):
    return {"week": week, "startDate": iso,
            "homeClassification": "fbs", "awayClassification": "fbs"}


def _cutovers(games):
    """{week: cutover} -- the same shape current_season_week walks."""
    return {wk: when for when, wk in app._week_switch_points(games)}


def _utc(dt):
    return dt.astimezone(timezone.utc).isoformat()


class TestWeekCutover(unittest.TestCase):
    def test_midweek_opener_does_not_outrun_the_previous_weekend(self):
        """THE BUG: week 6 (Tue opener) must not cut over before week 5 finishes."""
        games = [
            _g(5, "2026-10-01T23:45:00Z"),   # week 5 opens Thu Oct 1 (20:00 ET)
            _g(5, "2026-10-03T23:30:00Z"),   # ...and runs through Sat Oct 3
            _g(6, "2026-10-07T00:00:00Z"),   # week 6 opens Tue Oct 6 (20:00 ET)
        ]
        cuts = _cutovers(games)
        # Fri Oct 2 was the old (broken) answer; the Saturday slate was unplayed.
        self.assertEqual(_utc(cuts[6]), "2026-10-04T04:00:00+00:00")
        # and week 5 itself is unchanged
        self.assertEqual(_utc(cuts[5]), "2026-09-27T04:00:00+00:00")

    def test_standard_thursday_slate_is_unchanged(self):
        """The rule Jeff corrected on Sep 20 still yields Sunday 00:00 ET."""
        games = [
            _g(4, "2026-09-24T23:30:00Z"), _g(4, "2026-09-26T23:30:00Z"),
            _g(5, "2026-10-02T00:00:00Z"),
        ]
        cuts = _cutovers(games)
        # Sun Sep 27 00:00 ET -> 04:00Z, i.e. the day after week 4's last game
        self.assertEqual(_utc(cuts[5]), "2026-09-27T04:00:00+00:00")

    def test_labor_day_style_monday_finish_delays_the_next_week(self):
        """A Monday night finisher pushes the next cutover past Monday."""
        games = [
            _g(1, "2026-09-05T16:00:00Z"), _g(1, "2026-09-08T00:00:00Z"),  # Mon Sep 7
            _g(2, "2026-09-11T00:00:00Z"),                                  # Thu Sep 10
        ]
        cuts = _cutovers(games)
        # Tue Sep 8 00:00 ET -> 04:00Z, not the Sun Sep 6 the 4-day rule alone gives
        self.assertEqual(_utc(cuts[2]), "2026-09-08T04:00:00+00:00")

    def test_cutover_never_precedes_the_previous_weeks_last_kickoff(self):
        """The invariant, stated directly -- for every week in a messy schedule."""
        games = [
            _g(1, "2026-09-05T16:00:00Z"), _g(1, "2026-09-08T00:00:00Z"),
            _g(2, "2026-09-11T00:00:00Z"), _g(2, "2026-09-13T23:00:00Z"),
            _g(3, "2026-09-18T00:00:00Z"), _g(3, "2026-09-19T23:00:00Z"),
            _g(4, "2026-09-25T00:00:00Z"),
        ]
        pts = app._week_switch_points(games)
        last = {}
        for g in games:
            wk = g["week"]
            from datetime import datetime
            t = datetime.fromisoformat(g["startDate"].replace("Z", "+00:00"))
            last[wk] = max(last.get(wk, t), t)
        for cut, wk in pts:
            prev = last.get(wk - 1)
            if prev is not None:
                self.assertGreater(cut, prev,
                                   f"week {wk} became current before week {wk-1} finished")

    def test_degenerate_input_is_empty_not_an_error(self):
        self.assertEqual(app._week_switch_points([]), [])
        self.assertEqual(app._week_switch_points([{"week": None}]), [])
        self.assertEqual(app._week_switch_points([{"week": 3, "startDate": None}]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
