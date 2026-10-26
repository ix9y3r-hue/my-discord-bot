from pathlib import Path

path = Path("main.py")
source = path.read_text(encoding="utf-8")

# Keep Deno enabled for YouTube JavaScript challenges.
anchor = '    "fragment_retries": 3,\n'
deno_setting = '    # Let yt-dlp use Deno to solve YouTube JavaScript player challenges.\n    "js_runtimes": {"deno": {}},\n'
if '"js_runtimes": {"deno": {}}' not in source:
    if anchor not in source:
        raise SystemExit("ERROR: Could not find BASE_YTDL settings in main.py")
    source = source.replace(anchor, anchor + deno_setting, 1)

old_empty_queue = '''        if not queue:
            now_playing.pop(guild.id, None)
            await clear_panel(guild.id)
            return'''
new_empty_queue = '''        if not queue:
            now_playing.pop(guild.id, None)
            # Keep the last player panel and its controls visible while idle.
            return'''
if old_empty_queue in source:
    source = source.replace(old_empty_queue, new_empty_queue, 1)
elif new_empty_queue not in source:
    raise SystemExit("ERROR: Could not find play_next empty-queue block in main.py")

needle = '''async def advance_after_track(
    guild: discord.Guild,
    finished_track: Track,
    requester_channel: discord.abc.Messageable,
    finished_ok: bool,
) -> None:
    if now_playing.get(guild.id) is finished_track:'''
replacement = '''async def advance_after_track(
    guild: discord.Guild,
    finished_track: Track,
    requester_channel: discord.abc.Messageable,
    finished_ok: bool,
) -> None:
    # YouTube may return a signed media URL that the host cannot play (HTTP 403).
    # On a failed YouTube playback, try a SoundCloud copy once before moving on.
    if (
        not finished_ok
        and is_youtube_query(finished_track.get("webpage", ""))
        and not finished_track.get("soundcloud_fallback_tried")
    ):
        try:
            search_result = await extract_info(
                f"scsearch1:{finished_track.get('title', '')}", flat=True
            )
            if search_result and search_result.get("entries") is not None:
                search_result = next(
                    (entry for entry in search_result["entries"] if entry), None
                )
            if search_result:
                fallback_url = search_result.get("webpage_url") or search_result.get("url")
                if fallback_url and fallback_url.startswith("http"):
                    fallback_track: Track = {
                        "title": search_result.get("title") or finished_track.get("title", "أغنية"),
                        "webpage": fallback_url,
                        "requester": finished_track.get("requester", ""),
                        "soundcloud_fallback_tried": "1",
                    }
                    queues.setdefault(guild.id, []).insert(0, (fallback_track, requester_channel))
                    await send_safely(
                        requester_channel,
                        "يوتيوب رفض رابط الصوت؛ أجرب الآن نسخة بديلة من SoundCloud.",
                    )
        except Exception as fallback_error:
            logger.warning(
                "SoundCloud fallback failed for %s: %s",
                finished_track.get("title", "unknown track"),
                fallback_error,
            )

    if now_playing.get(guild.id) is finished_track:'''
if needle in source:
    source = source.replace(needle, replacement, 1)
elif "SoundCloud fallback failed for" not in source:
    raise SystemExit("ERROR: Could not find advance_after_track function in main.py")

path.write_text(source, encoding="utf-8")
print("Patched main.py: Deno enabled, player controls stay visible, and SoundCloud fallback added.")
