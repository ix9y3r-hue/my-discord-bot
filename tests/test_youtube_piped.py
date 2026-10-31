import unittest
from unittest.mock import patch

import youtube_piped


class PipedInstanceRegistryTests(unittest.TestCase):
    def test_registry_parser_returns_valid_api_urls_and_cdn_flags(self):
        registry = """
| Instance Name | Instance API URL | Instance Location(s) | CDN | Registered Users |
| --- | --- | --- | --- | --- |
| official | [https://pipedapi.example.org](https://pipedapi.example.org) | EU | Yes | |
| no-cdn | [https://api.no-cdn.example.net](https://api.no-cdn.example.net) | EU | No | |
| unsafe | [http://api.insecure.example](http://api.insecure.example) | EU | Yes | |
"""
        self.assertEqual(
            youtube_piped._parse_instance_registry(registry),
            [
                ("https://pipedapi.example.org", True),
                ("https://api.no-cdn.example.net", False),
            ],
        )

    def test_registry_parser_accepts_official_plain_markdown_table(self):
        # This is the format published by TeamPiped: table rows do not start
        # with a leading pipe character.
        registry = """
Instance Name | Instance API URL | Instance Location(s) | CDN | Registered Users
--- | --- | --- | --- | ---
kavin.rocks (Official) | https://pipedapi.kavin.rocks | US, IN | Yes | badge
leptons.xyz | https://pipedapi.leptons.xyz | AT | Yes | badge
privacy.com.de | https://piped-api.privacy.com.de | DE | No | badge
"""
        self.assertEqual(
            youtube_piped._parse_instance_registry(registry),
            [
                ("https://pipedapi.kavin.rocks", True),
                ("https://pipedapi.leptons.xyz", True),
                ("https://piped-api.privacy.com.de", False),
            ],
        )

    def test_registry_parser_deduplicates_api_urls(self):
        registry = """
| Name | API | Locations | CDN |
| --- | --- | --- | --- |
| one | https://api.example.org | EU | Yes |
| duplicate | https://api.example.org | EU | Yes |
"""
        self.assertEqual(
            youtube_piped._parse_instance_registry(registry),
            [("https://api.example.org", True)],
        )


class PipedMediaValidationTests(unittest.TestCase):
    def test_media_url_is_allowed_when_it_matches_response_proxy_url(self):
        self.assertTrue(
            youtube_piped._media_host_is_from_instance(
                "https://media-cdn.example.net/audio?token=secret",
                "https://api.piped.example.org",
                "https://media-cdn.example.net",
            )
        )

    def test_media_url_is_rejected_when_it_does_not_match_response_proxy_url(self):
        self.assertFalse(
            youtube_piped._media_host_is_from_instance(
                "https://attacker.example.net/audio?token=secret",
                "https://api.piped.example.org",
                "https://media-cdn.example.net",
            )
        )

    def test_get_audio_stream_uses_documented_proxy_url(self):
        api_base = "https://api.piped.example.org"
        payload = {
            "title": "Test track",
            "proxyUrl": "https://media-cdn.example.net",
            "audioStreams": [
                {
                    "bitrate": 128000,
                    "mimeType": "audio/mp4",
                    "videoOnly": False,
                    "url": "https://media-cdn.example.net/audio?token=secret",
                }
            ],
        }
        with (
            patch("youtube_piped._api_urls", return_value=[api_base]),
            patch("youtube_piped._request_json", return_value=payload),
        ):
            stream = youtube_piped.get_audio_stream(
                "https://www.youtube.com/watch?v=AbCdEf12345"
            )
        self.assertIsNotNone(stream)
        self.assertEqual(
            stream["url"],
            "https://media-cdn.example.net/audio?token=secret",
        )
        self.assertEqual(stream["api_base"], api_base)

    def test_stream_failure_log_identifies_stage_without_signed_query(self):
        api_base = "https://pipedapi.example.org"
        with (
            patch("youtube_piped._api_urls", return_value=[api_base]),
            patch(
                "youtube_piped._request_json",
                side_effect=OSError("request https://media.example.net/audio?token=secret failed"),
            ),
        ):
            with self.assertLogs(
                "discord_music_bot.youtube_piped", level="WARNING"
            ) as captured:
                result = youtube_piped.get_audio_stream(
                    "https://www.youtube.com/watch?v=AbCdEf12345"
                )
        self.assertIsNone(result)
        rendered = "\n".join(captured.output)
        self.assertIn("stream", rendered.lower())
        self.assertIn("pipedapi.example.org", rendered)
        self.assertNotIn("token=secret", rendered)


if __name__ == "__main__":
    unittest.main()
