"""One-shot Railway smoke test for the exact yt-dlp -> local file -> FFmpeg path.

This is only used by a temporary test service. It does not connect to Discord and
never prints cookie contents, signed media URLs, or yt-dlp's raw exception text.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from youtube_local_download import cleanup_local_download, download_youtube_audio

BGUTIL_SERVER = Path("/root/bgutil-ytdlp-pot-provider/server")
BGUTIL_NODE_MODULES = BGUTIL_SERVER / "node_modules"


def start_po_token_provider():
    """Start the local BgUtils provider on loopback for this one-shot test."""
    if not BGUTIL_NODE_MODULES.is_dir():
        raise RuntimeError("PO_TOKEN_PROVIDER_FILES_MISSING")
    process = subprocess.Popen(
        [
            "deno", "run", "--allow-env", "--allow-net", "--allow-ffi=.",
            "--allow-read=.", "../src/main.ts", "--host", "127.0.0.1",
        ],
        cwd=str(BGUTIL_NODE_MODULES),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import time
    import urllib.request

    for _ in range(20):
        if process.poll() is not None:
            raise RuntimeError("PO_TOKEN_PROVIDER_EXITED")
        try:
            with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=1):
                return process
        except Exception:
            time.sleep(1)
    process.terminate()
    raise RuntimeError("PO_TOKEN_PROVIDER_TIMEOUT")




def _failure_kind(error: BaseException) -> str:
    message = str(error).lower()
    if "confirm you're not a bot" in message or "sign in to confirm" in message:
        return "YOUTUBE_BOT_CHECK"
    if "http error 403" in message or "status code 403" in message:
        return "YOUTUBE_HTTP_403"
    if "po token" in message:
        return "YOUTUBE_PO_TOKEN"
    return type(error).__name__


def main() -> int:
    url = os.getenv("YOUTUBE_TEST_URL", "").strip()
    cookie_text = os.getenv("YOUTUBE_COOKIES", "")
    if not url:
        print(json.dumps({"status": "failed", "stage": "missing_test_url"}))
        return 2
    if not cookie_text.strip():
        print(json.dumps({"status": "failed", "stage": "missing_cookie_reference"}))
        return 2

    cookie_path = ""
    media = None
    provider = None
    try:
        provider = start_po_token_provider()
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="yt-smoke-", suffix=".txt", delete=False
        ) as cookie_file:
            cookie_file.write(cookie_text)
            cookie_path = cookie_file.name
        os.chmod(cookie_path, 0o600)

        options = {
            "format": "bestaudio[protocol^=m3u8_native]/bestaudio[protocol^=m3u8]/bestaudio",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "source_address": "0.0.0.0",
            "socket_timeout": 25,
            "retries": 1,
            "extractor_retries": 1,
            "fragment_retries": 1,
            "js_runtimes": {"deno": {}},
            "cookiefile": cookie_path,
        }
        media = download_youtube_audio(url, options)

        decoded = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", media["url"], "-f", "null", "-",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if decoded.returncode != 0:
            print(json.dumps({
                "status": "failed",
                "stage": "ffmpeg_decode",
                "ffmpeg_exit": decoded.returncode,
            }))
            return 1

        file_path = Path(media["url"])
        print(json.dumps({
            "status": "success",
            "stage": "yt_dlp_download_and_ffmpeg_decode",
            "bytes": file_path.stat().st_size,
            "extension": file_path.suffix.lower(),
            "duration_seconds": media.get("duration"),
        }))
        return 0
    except Exception as error:
        print(json.dumps({
            "status": "failed",
            "stage": _failure_kind(error),
            "exception_type": type(error).__name__,
        }))
        return 1
    finally:
        if media:
            cleanup_local_download(media.get("local_media_dir"))
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass
        if provider is not None and provider.poll() is None:
            provider.terminate()
            try:
                provider.wait(timeout=5)
            except subprocess.TimeoutExpired:
                provider.kill()


if __name__ == "__main__":
    raise SystemExit(main())
