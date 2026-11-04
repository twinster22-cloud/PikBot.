import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock
from live_props import normalize_prop, fetch_nba_props, LivePropsError

class LivePropsTests(unittest.TestCase):
    def test_fresh_recognized_prop(self):
        now=datetime(2026,11,4,20,0,tzinfo=timezone.utc)
        item=normalize_prop({"PlayerName":"Test Player","BetType":"Points","Value":22.5,
                             "Updated":"2026-11-04T19:55:00Z"},123,now)
        self.assertEqual(item["line"],22.5)
        self.assertEqual(item["stat"],"Points")

    def test_stale_or_missing_timestamp_is_rejected(self):
        now=datetime(2026,11,4,20,0,tzinfo=timezone.utc)
        for updated in [None,"2026-11-03T19:55:00Z"]:
            self.assertIsNone(normalize_prop({"PlayerName":"Test Player","BetType":"Points",
                             "Value":22.5,"Updated":updated},123,now))

    def test_unknown_stat_is_rejected(self):
        self.assertIsNone(normalize_prop({"PlayerName":"Test Player","BetType":"Unknown",
                             "Value":22.5,"Updated":"2026-11-04T19:55:00Z"},123,
                             datetime(2026,11,4,20,0,tzinfo=timezone.utc)))

    @patch.dict("os.environ",{},clear=True)
    def test_missing_credentials_fails_closed(self):
        with self.assertRaises(LivePropsError):
            fetch_nba_props(game_ids=["123"])

if __name__ == "__main__":
    unittest.main()
