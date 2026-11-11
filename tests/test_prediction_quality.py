"""Regression tests for prediction quality and the production recency gate.

Run with: python -m unittest discover -s tests -v
"""
import unittest
import pandas as pd
from engine import predict, outcome, evaluate


class PredictionQualityTests(unittest.TestCase):
    def make_data(self, count=20, days_old=1, value=12):
        as_of = pd.Timestamp("2026-11-11T18:00:00Z")
        dates = [as_of.normalize() - pd.Timedelta(days=days_old + i) for i in range(count)]
        history = pd.DataFrame([
            {"sport": "NBA", "player": "Example Player", "stat": "PTS", "date": day, "value": value}
            for day in dates
        ])
        offers = pd.DataFrame([{
            "sport": "NBA", "player": "Example Player", "stat": "PTS",
            "as_of": as_of, "line": 10.5, "event_id": "example-game"
        }])
        return history, offers

    def test_reject_19_games_live(self):
        h, o = self.make_data(count=19)
        self.assertTrue(predict(h, o, min_games=20, max_age_days=14).empty)

    def test_accept_20_recent_games_live(self):
        h, o = self.make_data(count=20)
        p = predict(h, o, min_games=20, max_age_days=14)
        self.assertEqual(set(p["side"]), {"MORE", "LESS"})
        self.assertTrue((p["games"] == 20).all())

    def test_reject_stale_history_live(self):
        h, o = self.make_data(days_old=16)
        self.assertTrue(predict(h, o, min_games=20, max_age_days=14).empty)

    def test_no_same_day_leakage(self):
        h, o = self.make_data(count=19)
        h.loc[len(h)] = ["NBA", "Example Player", "PTS", o.iloc[0]["as_of"].normalize(), 100]
        self.assertTrue(predict(h, o, min_games=20, max_age_days=14).empty)

    def test_duplicate_day_is_rejected(self):
        h, o = self.make_data()
        h.loc[len(h)] = ["NBA", "Example Player", "PTS", h.iloc[0]["date"], 999]
        self.assertTrue(predict(h, o, min_games=20, max_age_days=14).empty)

    def test_research_uses_separate_threshold(self):
        h, o = self.make_data(count=12, days_old=20)
        self.assertFalse(predict(h, o, min_games=12, max_age_days=30).empty)
        self.assertTrue(predict(h, o, min_games=20, max_age_days=14).empty)

    def test_invalid_offer_is_rejected(self):
        h, o = self.make_data()
        o.loc[0, "line"] = -1
        self.assertTrue(predict(h, o, min_games=20, max_age_days=14).empty)

    def test_grading(self):
        self.assertEqual(outcome(12, 10.5, "MORE"), "WIN")
        self.assertEqual(outcome(9, 10.5, "MORE"), "LOSS")
        self.assertEqual(outcome(10, 10, "LESS"), "PUSH")

    def test_no_forced_entries(self):
        h, o = self.make_data()
        picks = predict(h, o, min_games=20, max_age_days=14)
        self.assertTrue(evaluate(picks, min_prob=.99).empty)


if __name__ == "__main__":
    unittest.main()
