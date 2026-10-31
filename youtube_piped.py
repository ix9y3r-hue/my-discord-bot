"""Optional YouTube extraction fallback through public Piped API instances.

This module is deliberately stdlib-only. It is called only after the primary
yt-dlp path is blocked or a YouTube CDN URL fails during playback. No YouTube
cookies or Discord credentials are ever sent to Piped.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("discord_music_bot.youtube_piped")

DEFAULT_API_URLS = (
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.nosebs.ru",
)
USER_AGENT = "Mozilla/5.0 (compatible; DiscordMusicBot/1.0)"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
INSTANCE_REGISTRY_URL = (
    "https://raw.githubusercontent.com/TeamPiped/documentation/"
    "main/content/docs/public-instances/index.md"
)
INSTANCE_CACHE_TTL = 6 * 60 * 60
MAX_SEARCH_INSTANCES = 5
MAX_STREAM_INSTANCES = 3
_INSTANCE_CACHE_LOCK = threading.Lock()
_INSTANCE_CACHE: list[tuple[str, bool]] = []
_INSTANCE_CACHE_AT = 0.0


def _validated_api_url(candidate: str) -> str | None:
    candidate = str(candidate or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = f":{parsed.port}" if parsed.port else ""
    except (UnicodeError, ValueError):
        return None
    return f"https://{host}{port}"


def _parse_instance_registry(markdown: str) -> list[tuple[str, bool]]:
    """Parse the official public-instance Markdown table."""
    records: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for line in str(markdown or "").splitlines():
        line = line.strip()
        # TeamPiped's published Markdown table uses plain rows without
        # leading/trailing pipe characters; accept both common table styles.
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        urls = re.findall(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", cells[1])
        if not urls:
            continue
        api_url = _validated_api_url(urls[0])
        if not api_url or api_url in seen:
            continue
        cdn_value = cells[3].strip().lower()
        has_cdn = cdn_value in {"yes", "true", "1", "✓", "available"}
        records.append((api_url, has_cdn))
        seen.add(api_url)
    return records


def _load_instance_registry() -> list[tuple[str, bool]]:
    request = urllib.request.Request(
        INSTANCE_REGISTRY_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
    )
    with urllib.request.urlopen(request, timeout=4.0) as response:
        if getattr(response, "status", 200) != 200:
            raise OSError(f"Instance registry returned HTTP {response.status}")
        raw = response.read(200_001)
    if len(raw) > 200_000:
        raise ValueError("Piped instance registry exceeded the size limit")
    return _parse_instance_registry(raw.decode("utf-8"))


def _instance_records() -> list[tuple[str, bool]]:
    global _INSTANCE_CACHE, _INSTANCE_CACHE_AT
    now = time.monotonic()
    with _INSTANCE_CACHE_LOCK:
        if _INSTANCE_CACHE and now - _INSTANCE_CACHE_AT < INSTANCE_CACHE_TTL:
            return list(_INSTANCE_CACHE)
        try:
            records = _load_instance_registry()
            if not records:
                raise ValueError("No valid API instances found in official registry")
        except Exception as exc:
            logger.warning(
                "Piped instance discovery failed (error=%s); using built-in CDN list",
                type(exc).__name__,
            )
            records = [(url, True) for url in DEFAULT_API_URLS]
        _INSTANCE_CACHE = records
        _INSTANCE_CACHE_AT = now
        return list(records)


def _url_host(value: str) -> str:
    try:
        return (urllib.parse.urlparse(value).hostname or "unknown").lower()
    except ValueError:
        return "unknown"


def _error_kind(exc: BaseException) -> str:
    status = getattr(exc, "code", None)
    if isinstance(status, int):
        return f"http_{status}"
    return type(exc).__name__


def extract_video_id(value: str) -> str | None:
    """Extract an 11-character YouTube video ID from a supported URL."""
    value = str(value or "").strip()
    if VIDEO_ID_RE.fullmatch(value):
        return value
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    video_id = ""
    if host == "youtu.be" or host.endswith(".youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    else:
        video_id = (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]
        if not video_id:
            parts = [part for part in parsed.path.split("/") if part]
            for marker in ("embed", "shorts", "live", "v"):
                if marker in parts:
                    index = parts.index(marker)
                    if index + 1 < len(parts):
                        video_id = parts[index + 1]
                        break

    return video_id if VIDEO_ID_RE.fullmatch(video_id) else None


def _api_urls(
    preferred: str | None = None,
    *,
    require_cdn: bool = False,
) -> list[str]:
    configured = os.getenv("PIPED_API_URLS", "").strip()
    if configured:
        # An explicit operator override is trusted as the intended instance list.
        records = [
            (_validated_api_url(part), True)
            for part in re.split(r"[;,]", configured)
            if part.strip()
        ]
        records = [(url, cdn) for url, cdn in records if url]
    else:
        records = _instance_records()
        if require_cdn:
            records = [(url, cdn) for url, cdn in records if cdn]
        if not records:
            records = [(url, True) for url in DEFAULT_API_URLS]

    preferred_url = _validated_api_url(preferred) if preferred else None
    urls = [url for url, _cdn in records]
    if preferred_url and preferred_url in urls:
        urls.remove(preferred_url)
        urls.insert(0, preferred_url)

    limit = MAX_STREAM_INSTANCES if require_cdn else MAX_SEARCH_INSTANCES
    result = list(dict.fromkeys(urls))[:limit]
    return result or list(DEFAULT_API_URLS)


def _request_json(api_base: str, path: str, params: dict[str, str] | None = None) -> Any:
    url = api_base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    timeout = 3.5
    try:
        timeout = float(os.getenv("PIPED_API_TIMEOUT", "3.5"))
    except (TypeError, ValueError):
        pass
    timeout = max(2.0, min(timeout, 8.0))

    with urllib.request.urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise OSError(f"Piped API returned HTTP {response.status}")
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Piped API response exceeded the size limit")
    return json.loads(raw.decode("utf-8"))


def search_video(query: str) -> dict[str, Any] | None:
    """Search a Piped instance and return the first YouTube video result."""
    query = str(query or "").strip()
    if not query or len(query) > 300:
        return None

    for api_base in _api_urls():
        try:
            payload = _request_json(
                api_base,
                "/search",
                {"q": query, "filter": "videos"},
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                logger.warning(
                    "Piped search response has invalid schema (api_host=%s)",
                    _url_host(api_base),
                )
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") not in (None, "stream"):
                    continue
                video_id = (
                    extract_video_id(str(item.get("url") or ""))
                    or extract_video_id(str(item.get("id") or ""))
                    or extract_video_id(str(item.get("videoId") or ""))
                )
                if not video_id:
                    continue
                return {
                    "video_id": video_id,
                    "webpage": f"https://www.youtube.com/watch?v={video_id}",
                    "title": str(item.get("title") or query),
                    "thumbnail": str(item.get("thumbnail") or ""),
                    "duration": item.get("duration"),
                    "uploader": str(item.get("uploader") or ""),
                    "api_base": api_base,
                }
        except Exception as exc:
            logger.warning(
                "Piped search request failed (api_host=%s, error=%s)",
                _url_host(api_base),
                _error_kind(exc),
            )
    logger.info("Piped title search found no usable video")
    return None


def _media_host_is_from_instance(
    media_url: str,
    api_base: str,
    proxy_url: str | None = None,
) -> bool:
    try:
        media = urllib.parse.urlparse(media_url)
        api = urllib.parse.urlparse(api_base)
    except ValueError:
        return False
    if (
        media.scheme != "https"
        or not media.hostname
        or not api.hostname
        or media.username
        or media.password
    ):
        return False

    # Piped's documented /streams response includes proxyUrl. Do not infer
    # trust from the last two hostname labels: public suffixes such as
    # .com.de would allow unrelated hosts to pass that weak check.
    if not proxy_url:
        return False
    try:
        proxy = urllib.parse.urlparse(proxy_url)
    except ValueError:
        return False
    if (
        proxy.scheme != "https"
        or not proxy.hostname
        or proxy.username
        or proxy.password
        or proxy.path not in ("", "/")
        or proxy.query
        or proxy.fragment
    ):
        return False
    media_port = media.port or 443
    proxy_port = proxy.port or 443
    return (
        media.hostname.lower() == proxy.hostname.lower()
        and media_port == proxy_port
    )


def get_audio_stream(
    youtube_url: str,
    preferred_api: str | None = None,
) -> dict[str, Any] | None:
    """Get an audio-only stream through the instance's documented proxy host."""
    video_id = extract_video_id(youtube_url)
    if not video_id:
        return None

    for api_base in _api_urls(preferred_api, require_cdn=True):
        try:
            payload = _request_json(api_base, f"/streams/{video_id}")
            if not isinstance(payload, dict):
                logger.warning(
                    "Piped stream response has invalid schema (api_host=%s)",
                    _url_host(api_base),
                )
                continue
            audio_streams = payload.get("audioStreams", [])
            if not isinstance(audio_streams, list):
                logger.warning(
                    "Piped stream response has no audioStreams list (api_host=%s)",
                    _url_host(api_base),
                )
                continue

            proxy_url = payload.get("proxyUrl")
            candidates: list[tuple[int, dict[str, Any]]] = []
            rejected_hosts = 0
            for item in audio_streams:
                if not isinstance(item, dict) or item.get("videoOnly") is True:
                    continue
                media_url = item.get("url")
                if (
                    not isinstance(media_url, str)
                    or not _media_host_is_from_instance(
                        media_url, api_base, str(proxy_url) if proxy_url else None
                    )
                ):
                    rejected_hosts += 1
                    continue
                mime_type = str(item.get("mimeType") or "").lower()
                if mime_type and not mime_type.startswith("audio/"):
                    continue
                try:
                    bitrate = int(item.get("bitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                candidates.append((bitrate, item))

            if not candidates:
                logger.warning(
                    "Piped stream had no usable audio (api_host=%s, proxy_host=%s, "
                    "audio_count=%d, rejected_hosts=%d)",
                    _url_host(api_base),
                    _url_host(str(proxy_url)) if proxy_url else "missing",
                    len(audio_streams),
                    rejected_hosts,
                )
                continue
            _, selected = max(candidates, key=lambda entry: entry[0])
            return {
                "url": selected["url"],
                "http_headers": {"User-Agent": USER_AGENT},
                "title": str(payload.get("title") or ""),
                "thumbnail": str(payload.get("thumbnailUrl") or ""),
                "duration": payload.get("duration"),
                "uploader": str(payload.get("uploader") or ""),
                "api_base": api_base,
            }
        except Exception as exc:
            logger.warning(
                "Piped stream request failed (api_host=%s, error=%s)",
                _url_host(api_base),
                _error_kind(exc),
            )
    logger.warning("Piped stream failed for video_id=%s on all eligible CDN instances", video_id)
    return None
