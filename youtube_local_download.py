"""Download YouTube media with yt-dlp before handing it to FFmpeg.

YouTube media URLs are short-lived and can be bound to the request headers/session
that created them. FFmpeg opening such a URL independently may receive HTTP 403.
This helper lets yt-dlp perform the media request and gives FFmpeg a local file.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable


def _candidate_paths(info: Any) -> list[str]:
    """Collect file paths reported by yt-dlp without trusting arbitrary URLs."""
    if not isinstance(info, dict):
        return []
    candidates: list[str] = []
    for key in ("filepath", "_filename", "filename"):
        value = info.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if not isinstance(item, dict):
                continue
            for key in ("filepath", "_filename", "filename"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)
    return candidates


def download_youtube_audio(
    url: str,
    ydl_options: dict[str, Any] | None = None,
    *,
    ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Download one YouTube track and return metadata with a local audio path.

    The caller supplies the same yt-dlp format, JS-runtime, PO-token and cookie
    options used by the main extractor. A private temporary directory is created
    for every download and must be removed with cleanup_local_download().
    """
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("A valid YouTube URL is required")

    if ydl_factory is None:
        import yt_dlp  # Imported lazily so this helper can be unit-tested alone.
        ydl_factory = yt_dlp.YoutubeDL

    work_dir = tempfile.mkdtemp(prefix="discord-youtube-")
    options = dict(ydl_options or {})
    options.update(
        {
            "outtmpl": os.path.join(work_dir, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "overwrites": True,
            "continuedl": False,
            "nopart": True,
        }
    )

    ydl = None
    try:
        ydl = ydl_factory(options)
        info = ydl.extract_info(url, download=True)

        filepath = next(
            (
                path
                for path in _candidate_paths(info)
                if os.path.isfile(path) and os.path.getsize(path) > 0
            ),
            None,
        )
        if filepath is None:
            # Some extractor/download combinations omit filepath in their
            # returned info even though the file was written successfully.
            files = [
                path
                for path in Path(work_dir).iterdir()
                if path.is_file()
                and not path.name.endswith((".part", ".ytdl", ".json"))
                and path.stat().st_size > 0
            ]
            if len(files) == 1:
                filepath = str(files[0])

        if filepath is None:
            raise RuntimeError("yt-dlp finished without producing a local audio file")

        result = dict(info) if isinstance(info, dict) else {}
        result.update(
            {
                "url": os.path.abspath(filepath),
                "local_file": True,
                "local_media_dir": work_dir,
            }
        )
        return result
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    finally:
        if ydl is not None:
            close = getattr(ydl, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def cleanup_local_download(directory: str | None) -> None:
    """Remove one temporary download directory; safe to call more than once."""
    if isinstance(directory, str) and directory:
        shutil.rmtree(directory, ignore_errors=True)
