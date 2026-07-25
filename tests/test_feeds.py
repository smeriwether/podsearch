from __future__ import annotations

import unittest

from podsearch.feeds import parse_feed


class FeedTests(unittest.TestCase):
    def test_rss_episode_parsing(self) -> None:
        parsed = parse_feed(
            b"""<?xml version="1.0"?>
            <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" version="2.0">
              <channel>
                <title>Example Show</title>
                <item>
                  <title>Example Episode</title>
                  <guid>episode-1</guid>
                  <link>https://example.com/episodes/1</link>
                  <description><![CDATA[<p>Hello &amp; welcome.</p>]]></description>
                  <pubDate>Sat, 25 Jul 2026 12:00:00 GMT</pubDate>
                  <itunes:duration>01:02:03</itunes:duration>
                  <enclosure url="https://example.com/1.mp3" type="audio/mpeg"/>
                </item>
              </channel>
            </rss>""",
            "Fallback",
        )
        self.assertEqual(parsed.title, "Example Show")
        self.assertEqual(len(parsed.episodes), 1)
        episode = parsed.episodes[0]
        self.assertEqual(episode["description"], "Hello & welcome.")
        self.assertEqual(episode["duration"], "01:02:03")
        self.assertEqual(episode["audio_url"], "https://example.com/1.mp3")
        self.assertEqual(episode["published_at"], "2026-07-25T12:00:00+00:00")
