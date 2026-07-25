from __future__ import annotations

import unittest

from podsearch.apple import extended_chart_rankings, favorite_apple_ids


class AppleTests(unittest.TestCase):
    def test_favorites_accept_ids_and_urls(self) -> None:
        self.assertEqual(
            favorite_apple_ids(
                (
                    "1322200189",
                    "https://podcasts.apple.com/us/podcast/crime-junkie/id1322200189",
                    "https://podcasts.apple.com/us/podcast/the-daily/id1200361736?i=1",
                )
            ),
            ("1322200189", "1200361736"),
        )

    def test_invalid_favorite_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            favorite_apple_ids(("Crime Junkie",))

    def test_extended_chart_rankings_uses_apple_feed_order(self) -> None:
        payload = {
            "feed": {
                "entry": [
                    {"id": {"attributes": {"im:id": "one"}}},
                    {"id": {"attributes": {"im:id": "two"}}},
                ]
            }
        }
        self.assertEqual(extended_chart_rankings(payload), {"one": 1, "two": 2})
