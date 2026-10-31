import unittest
from unittest.mock import patch

import youtube_piped


class PipedCooldownTests(unittest.TestCase):
    def setUp(self):
        youtube_piped._reset_stream_failure_cooldown()

    def test_failed_instance_list_is_not_retried_during_cooldown(self):
        api = "https://pipedapi.example.org"
        with (
            patch("youtube_piped._api_urls", return_value=[api]) as instances,
            patch("youtube_piped._request_json", side_effect=OSError("offline")) as request,
            patch("youtube_piped.time.monotonic", return_value=100.0),
        ):
            url = "https://www.youtube.com/watch?v=AbCdEf12345"
            self.assertIsNone(youtube_piped.get_audio_stream(url))
            self.assertIsNone(youtube_piped.get_audio_stream(url))
        self.assertEqual(instances.call_count, 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(youtube_piped._stream_failure_cooldown_remaining(), 300)

    def test_success_clears_previous_cooldown(self):
        with youtube_piped._STREAM_FAILURE_LOCK:
            youtube_piped._STREAM_FAILURE_UNTIL = 200.0
        with patch("youtube_piped.time.monotonic", return_value=100.0):
            self.assertEqual(youtube_piped._stream_failure_cooldown_remaining(), 100)
        youtube_piped._reset_stream_failure_cooldown()
        self.assertEqual(youtube_piped._stream_failure_cooldown_remaining(), 0)


if __name__ == "__main__":
    unittest.main()
