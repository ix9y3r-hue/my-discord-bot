import os
import tempfile
import unittest
from pathlib import Path

from youtube_local_download import cleanup_local_download, download_youtube_audio


class FakeYoutubeDL:
    def __init__(self, options, *, produce_file=True):
        self.options = options
        self.produce_file = produce_file
        self.download_calls = []
        self.closed = False

    def extract_info(self, url, download=False):
        self.download_calls.append((url, download))
        if not self.produce_file:
            return {"id": "video123", "title": "Test audio"}
        output = self.options["outtmpl"].replace("%(id)s", "video123").replace("%(ext)s", "webm")
        Path(output).write_bytes(b"test-audio")
        return {
            "id": "video123",
            "title": "Test audio",
            "requested_downloads": [{"filepath": output, "format_id": "251"}],
        }

    def close(self):
        self.closed = True


class YoutubeLocalDownloadTests(unittest.TestCase):
    def test_downloads_with_yt_dlp_and_returns_a_local_file(self):
        instances = []

        def factory(options):
            instance = FakeYoutubeDL(options)
            instances.append(instance)
            return instance

        result = download_youtube_audio(
            "https://www.youtube.com/watch?v=AbCdEf12345",
            {"cookiefile": "/private/cookies.txt", "js_runtimes": {"deno": {}}},
            ydl_factory=factory,
        )
        try:
            self.assertEqual(result["title"], "Test audio")
            self.assertTrue(result["local_file"])
            self.assertTrue(os.path.isabs(result["url"]))
            self.assertEqual(Path(result["url"]).read_bytes(), b"test-audio")
            self.assertEqual(instances[0].download_calls, [
                ("https://www.youtube.com/watch?v=AbCdEf12345", True)
            ])
            self.assertEqual(instances[0].options["cookiefile"], "/private/cookies.txt")
            self.assertEqual(instances[0].options["js_runtimes"], {"deno": {}})
            self.assertTrue(instances[0].options["nopart"])
            self.assertTrue(instances[0].options["noplaylist"])
            self.assertTrue(instances[0].closed)
        finally:
            cleanup_local_download(result["local_media_dir"])

        self.assertFalse(os.path.exists(result["local_media_dir"]))

    def test_missing_output_fails_and_cleans_temporary_directory(self):
        created = []

        def factory(options):
            created.append(os.path.dirname(options["outtmpl"]))
            return FakeYoutubeDL(options, produce_file=False)

        with self.assertRaisesRegex(RuntimeError, "without producing"):
            download_youtube_audio(
                "https://www.youtube.com/watch?v=AbCdEf12345",
                ydl_factory=factory,
            )
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))

    def test_rejects_non_http_input_before_creating_a_directory(self):
        with self.assertRaises(ValueError):
            download_youtube_audio("javascript:alert(1)", ydl_factory=FakeYoutubeDL)

    def test_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "download")
            os.mkdir(target)
            Path(target, "audio.webm").write_bytes(b"audio")
            cleanup_local_download(target)
            cleanup_local_download(target)
            self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
