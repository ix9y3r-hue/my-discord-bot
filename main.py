import asyncio
import json
import time
import logging
import os
import shlex
import shutil
import socket
from threading import Thread
from typing import Any

import discord
import yt_dlp
from discord.ext import commands, tasks
from flask import Flask

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "!"
STATS_FILE = os.getenv("VOICE_STATS_FILE", "voice_stats.json")
STATS_WEEK_SECONDS = 7 * 24 * 60 * 60
STATS_UPDATE_SECONDS = 10 * 60
MAX_QUEUE = 50
EXTRACT_TIMEOUT = 45

SHORT_COMMANDS = {"ش", "وقف", "ك", "كمل", "س", "سكب", "كرر"}

Track = dict[str, str]


def get_command_prefix(_bot: commands.Bot, message: discord.Message) -> str | list[str]:
    parts = message.content.strip().split(maxsplit=1)
    if parts and parts[0] in SHORT_COMMANDS:
        return ""
    # Keep existing ! prefix commands; setup is registered as a Discord slash command.
    return PREFIX


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger("discord_music_bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True


class MusicBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Load the temporary voice-room feature before connecting to Discord.
        await self.load_extension("temp_voice")


bot = MusicBot(command_prefix=get_command_prefix, intents=intents)

BASE_YTDL = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    # Railway/container IPv4 is generally more reliable for media hosts.
    "source_address": "0.0.0.0",
    "socket_timeout": 25,
    "retries": 3,
    "extractor_retries": 3,
    "fragment_retries": 3,
    # Let yt-dlp use Deno to solve YouTube JavaScript player challenges.
    "js_runtimes": {"deno": {}},
}
ytdl_search = yt_dlp.YoutubeDL({**BASE_YTDL, "extract_flat": "in_playlist"})
ytdl_stream = yt_dlp.YoutubeDL(BASE_YTDL)
# Try different official YouTube player clients if the default extraction fails.
# They have different format availability, so trying them separately is more useful
# than combining them into one extractor configuration.
YOUTUBE_FALLBACK_CLIENTS = ("tv", "android_vr", "web_safari")
ytdl_search_fallbacks = [
    yt_dlp.YoutubeDL({
        **BASE_YTDL,
        "extract_flat": "in_playlist",
        "extractor_args": {"youtube": {"player_client": [client]}},
    })
    for client in YOUTUBE_FALLBACK_CLIENTS
]
ytdl_stream_fallbacks = [
    yt_dlp.YoutubeDL({
        **BASE_YTDL,
        "extractor_args": {"youtube": {"player_client": [client]}},
    })
    for client in YOUTUBE_FALLBACK_CLIENTS
]
ytdl_lock = asyncio.Lock()

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS_AFTER = "-vn"

queues: dict[int, list[tuple[Track, discord.abc.Messageable]]] = {}
now_playing: dict[int, Track] = {}
repeat_guilds: set[int] = set()
queue_repeat_guilds: set[int] = set()
skipped_guilds: set[int] = set()
last_announced: dict[int, Track] = {}
history: dict[int, list[Track]] = {}
volumes: dict[int, float] = {}
panels: dict[int, discord.Message] = {}
DEFAULT_VOLUME = 0.5
MIN_VOLUME = 0.1
MAX_VOLUME = 1.5
VOLUME_STEP = 0.1
voice_locks: dict[int, asyncio.Lock] = {}
voice_connection_locks: dict[int, asyncio.Lock] = {}
STATE_FILE = os.getenv("STATE_FILE", "voice_state.json")
ROLE_OWNERS_FILE = os.getenv("ROLE_OWNERS_FILE", "created_roles.json")


def load_targets() -> dict[int, int]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


voice_targets: dict[int, int] = load_targets()


def load_created_roles() -> dict[int, dict[int, int]]:
    try:
        with open(ROLE_OWNERS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return {
            int(guild_id): {int(user_id): int(role_id) for user_id, role_id in users.items()}
            for guild_id, users in raw.items()
            if isinstance(users, dict)
        }
    except (OSError, ValueError, TypeError):
        return {}


def save_created_roles() -> None:
    try:
        temp_path = f"{ROLE_OWNERS_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(
                {str(guild_id): {str(user_id): role_id for user_id, role_id in users.items()}
                 for guild_id, users in created_roles.items()},
                f,
                ensure_ascii=False,
            )
        os.replace(temp_path, ROLE_OWNERS_FILE)
    except OSError:
        logger.exception("Could not save created role ownership")


created_roles: dict[int, dict[int, int]] = load_created_roles()


def load_voice_stats() -> dict[int, dict[str, Any]]:
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        result: dict[int, dict[str, Any]] = {}
        for guild_key, value in raw.items():
            if not isinstance(value, dict):
                continue
            result[int(guild_key)] = {
                "channel_id": int(value["channel_id"]) if value.get("channel_id") else None,
                "period_start": float(value.get("period_start", time.time())),
                "last_tick": float(value.get("last_tick", time.time())),
                "last_report": float(value.get("last_report", value.get("period_start", time.time()))),
                "message_ids": [int(mid) for mid in value.get("message_ids", []) if str(mid).isdigit()],
                "seconds": {str(k): max(0.0, float(v)) for k, v in value.get("seconds", {}).items()},
            }
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def save_voice_stats() -> None:
    try:
        temp_path = f"{STATS_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in voice_stats.items()}, f, ensure_ascii=False)
        os.replace(temp_path, STATS_FILE)
    except OSError:
        logger.exception("Could not save voice activity stats")


voice_stats: dict[int, dict[str, Any]] = load_voice_stats()
stats_active_channels: dict[int, set[int]] = {}


def excluded_temporary_voice_channel_ids(guild: discord.Guild) -> set[int]:
    """Return temporary voice rooms and their generator channel, which are excluded from stats."""
    excluded: set[int] = set()
    cog = bot.get_cog("TemporaryVoice")
    if cog is not None:
        excluded.update(getattr(cog, "temporary_channel_ids", set()))
        settings = getattr(cog, "config", {}).get(guild.id, {})
        generator_id = settings.get("generator_channel_id")
        if generator_id:
            excluded.add(int(generator_id))

    # Also recognize temporary rooms by their stable name prefix, including during
    # the short period before the extension rebuilds its registry after a restart.
    for channel in guild.channels:
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            if channel.name.startswith("🔊・"):
                excluded.add(channel.id)
    return excluded


def active_human_voice_channels(guild: discord.Guild) -> set[int]:
    """Return active non-bot voice/stage channels, excluding temporary rooms."""
    excluded = excluded_temporary_voice_channel_ids(guild)
    active: set[int] = set()
    for channel in guild.channels:
        if (
            isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
            and channel.id not in excluded
            and any(not member.bot for member in channel.members)
        ):
            active.add(channel.id)
    return active


def format_activity(seconds: float) -> str:
    total_minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} ساعة و{minutes} دقيقة"


async def publish_weekly_stats(guild: discord.Guild, record: dict[str, Any]) -> None:
    channel_id = record.get("channel_id")
    target = guild.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(target, discord.TextChannel):
        logger.warning("Weekly voice stats channel is missing for guild %s", guild.id)
        return

    excluded_channel_ids = excluded_temporary_voice_channel_ids(guild)
    # Ignore any old activity accumulated for temporary rooms before this fix.
    totals = {
        str(k): float(v)
        for k, v in record.get("seconds", {}).items()
        if int(k) not in excluded_channel_ids
    }
    channel_ids = {
        channel.id for channel in guild.channels
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
        and channel.id not in excluded_channel_ids
    } | {int(k) for k in totals}
    ranked = sorted(
        ((voice_channel_id, totals.get(str(voice_channel_id), 0.0)) for voice_channel_id in channel_ids),
        key=lambda item: item[1],
        reverse=True,
    )[:3]

    medals = {1: "🎖️", 2: "🥈", 3: "🥉"}
    rank_names = {1: "المركز الأول", 2: "المركز الثاني", 3: "المركز الثالث"}
    lines: list[str] = []
    for rank, (voice_channel_id, seconds) in enumerate(ranked, 1):
        voice_channel = guild.get_channel(voice_channel_id)
        name = voice_channel.name if voice_channel else f"روم محذوف ({voice_channel_id})"
        lines.append(
            f"## {medals[rank]} {rank_names[rank]}\n"
            f"# 🔊 {name}\n"
            f"**⏱️ مدة النشاط: {format_activity(seconds)}**"
        )

    # إجمالي نشاط الرومات الداخلة في الترتيب فقط، باستثناء الرومات المؤقتة.
    total_seconds = sum(totals.values())
    description = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(lines) if lines else "## لا توجد رومات صوتية مسجلة."

    period_start = float(record.get("period_start", time.time()))
    period_end = period_start + STATS_WEEK_SECONDS
    message_ids = list(record.get("message_ids", []))
    embed = discord.Embed(
        title="🏆 أفضل 3 رومات صوتية نشاطًا",
        description=description,
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="📊 إجمالي نشاط جميع الرومات",
        value=f"# {format_activity(total_seconds)}",
        inline=False,
    )
    embed.set_footer(
        text="تحديث الترتيب كل 10 دقائق • تصفير الإحصائيات كل 7 أيام • الرومات المؤقتة وقناة إنشائها غير مشمولة"
    )

    new_message_ids: list[int] = []
    try:
        message = None
        if message_ids:
            try:
                message = await target.fetch_message(int(message_ids[0]))
                await message.edit(embed=embed)
            except discord.NotFound:
                message = None
        if message is None:
            message = await target.send(embed=embed)
        new_message_ids.append(message.id)
    except discord.DiscordException:
        logger.exception("Could not publish voice stats")
        return

    # Remove extra pages created by an older version, if any.
    for stale_id in message_ids[1:]:
        try:
            stale = await target.fetch_message(int(stale_id))
            await stale.delete()
        except discord.DiscordException:
            pass
    record["message_ids"] = new_message_ids
    save_voice_stats()


def set_target(guild_id: int, channel_id: int | None) -> None:
    if channel_id is None:
        voice_targets.pop(guild_id, None)
    else:
        voice_targets[guild_id] = channel_id
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in voice_targets.items()}, f)
    except OSError:
        logger.warning("Could not save voice state")


voice_reconnect_tasks: dict[int, asyncio.Task[None]] = {}


def is_youtube_query(query: str) -> bool:
    lowered = query.lower()
    return (
        any(host in lowered for host in (
            "youtube.com", "youtu.be", "youtube-nocookie.com"
        ))
        or lowered.startswith(("ytsearch", "youtube:"))
    )


async def extract_info(query: str, *, flat: bool = False) -> dict[str, Any] | None:
    default_ydl = ytdl_search if flat else ytdl_stream
    fallback_clients = ytdl_search_fallbacks if flat else ytdl_stream_fallbacks
    async with ytdl_lock:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(default_ydl.extract_info, query, download=False),
                timeout=EXTRACT_TIMEOUT,
            )
        except Exception as first_error:
            if not is_youtube_query(query):
                raise
            logger.warning(
                "Default YouTube extraction failed; trying alternate player clients: %s",
                first_error,
            )
            last_error: Exception = first_error
            for client, fallback_ydl in zip(YOUTUBE_FALLBACK_CLIENTS, fallback_clients):
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            fallback_ydl.extract_info, query, download=False
                        ),
                        timeout=EXTRACT_TIMEOUT,
                    )
                    if result:
                        logger.info("YouTube extraction succeeded using client %s", client)
                        return result
                except Exception as fallback_error:
                    last_error = fallback_error
                    logger.warning(
                        "YouTube extraction with client %s failed: %s",
                        client, fallback_error,
                    )
            # Keep the original exception as the final error: it is usually the
            # clearest explanation of the initial failure (e.g. bot verification).
            raise first_error


def build_queries(search: str) -> list[str]:
    if search.startswith(("http://", "https://")):
        return [search]
    return [f"scsearch1:{search}", f"ytsearch1:{search}"]


async def find_track(search: str) -> Track:
    last_error: Exception | None = None
    for query in build_queries(search):
        try:
            data = await extract_info(query, flat=True)
        except Exception as exc:
            last_error = exc
            logger.warning("Search failed for %s: %s", query, exc)
            continue
        if data and data.get("entries") is not None:
            data = next((e for e in data["entries"] if e), None)
        if not data:
            continue
        webpage = data.get("webpage_url") or data.get("url")
        if not webpage:
            continue
        return {"title": data.get("title") or "أغنية بدون اسم", "webpage": webpage}
    if last_error:
        logger.error("All searches failed", exc_info=last_error)
    raise ValueError("ما لقيت الأغنية. جرّب اسمًا ثانيًا.")


async def send_safely(channel: discord.abc.Messageable, message: str) -> None:
    try:
        await channel.send(message)
    except discord.DiscordException:
        logger.exception("Could not send a message to the request channel")


def build_ffmpeg_source(
    stream: dict[str, Any], volume: float
) -> discord.PCMVolumeTransformer:
    before = FFMPEG_BEFORE
    headers = stream.get("http_headers") or {}
    if headers:
        header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        before += f" -headers {shlex.quote(header_str)}"
    source = discord.FFmpegPCMAudio(
        stream["url"], before_options=before, options=FFMPEG_OPTIONS_AFTER
    )
    return discord.PCMVolumeTransformer(source, volume=volume)


def format_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def queue_text(guild_id: int) -> str:
    queue = queues.get(guild_id, [])
    current = now_playing.get(guild_id)
    heading = f"🎵 تعمل الآن: **{current['title']}**\n" if current else ""
    if not queue:
        return heading or "قائمة الانتظار فارغة."
    lines = [
        f"{index}. {track['title']}"
        for index, (track, _channel) in enumerate(queue[:15], 1)
    ]
    extra = f"\n… و{len(queue) - 15} أخرى" if len(queue) > 15 else ""
    return heading + "**قائمة الانتظار:**\n" + "\n".join(lines) + extra


async def clear_panel(guild_id: int) -> None:
    """يشيل الأزرار من اللوحة القديمة عشان ما تبقى أزرار ميتة."""
    message = panels.pop(guild_id, None)
    if message:
        try:
            await message.edit(view=None)
        except discord.DiscordException:
            pass


QUEUE_PAGE_SIZE = 4


class RemoveTrackButton(discord.ui.Button):
    """زر ❌ جنبه اسم الأغنية؛ يشيلها من الطابور."""

    def __init__(self, number: int, track: Track, row: int) -> None:
        title = track["title"]
        label = f"{number}. {title}"
        if len(label) > 70:
            label = label[:67] + "..."
        super().__init__(
            label=label,
            emoji="❌",
            style=discord.ButtonStyle.danger,
            row=row,
        )
        self.track = track

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, QueueView):
            await view.remove_track(interaction, self.track)


class QueueView(discord.ui.View):
    def __init__(self, guild_id: int, page: int = 0) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.page = page
        self._build()

    def _total_pages(self) -> int:
        count = len(queues.get(self.guild_id, []))
        return max(1, -(-count // QUEUE_PAGE_SIZE))

    def _build(self) -> None:
        self.clear_items()
        queue = queues.get(self.guild_id, [])
        pages = self._total_pages()
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * QUEUE_PAGE_SIZE
        for offset, (track, _channel) in enumerate(
            queue[start:start + QUEUE_PAGE_SIZE]
        ):
            self.add_item(RemoveTrackButton(start + offset + 1, track, offset))
        if pages > 1:
            prev_button = discord.ui.Button(
                emoji="◀️",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=self.page == 0,
            )
            prev_button.callback = self._prev_page
            page_button = discord.ui.Button(
                label=f"{self.page + 1}/{pages}",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=True,
            )
            next_button = discord.ui.Button(
                emoji="▶️",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=self.page >= pages - 1,
            )
            next_button.callback = self._next_page
            self.add_item(prev_button)
            self.add_item(page_button)
            self.add_item(next_button)

    def content(self) -> str:
        queue = queues.get(self.guild_id, [])
        current = now_playing.get(self.guild_id)
        heading = f"🎵 تعمل الآن: **{current['title']}**\n" if current else ""
        if not queue:
            return heading + "قائمة الانتظار فارغة."
        return (
            heading
            + f"📜 **قائمة الانتظار** ({len(queue)} أغنية)\n"
            + "اضغط ❌ جنب الأغنية عشان تشيلها."
        )

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        if self.children:
            await interaction.response.edit_message(
                content=self.content(), view=self
            )
        else:
            await interaction.response.edit_message(
                content=self.content(), view=None
            )

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        await self._refresh(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self._refresh(interaction)

    async def remove_track(
        self, interaction: discord.Interaction, track: Track
    ) -> None:
        queue = queues.get(self.guild_id, [])
        for index, (queued_track, _channel) in enumerate(queue):
            if queued_track is track:
                del queue[index]
                break
        await self._refresh(interaction)


class PlayerView(discord.ui.View):
    def __init__(self, guild_id: int, embed: discord.Embed) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.embed = embed
        if guild_id in repeat_guilds:
            self.repeat_button.style = discord.ButtonStyle.success
            self.repeat_button.label = "تكرار الأغنية"
        elif guild_id in queue_repeat_guilds:
            self.repeat_button.style = discord.ButtonStyle.primary
            self.repeat_button.label = "تكرار الطابور"
        else:
            self.repeat_button.style = discord.ButtonStyle.secondary
            self.repeat_button.label = "التكرار متوقف"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        voice_client = interaction.guild.voice_client if interaction.guild else None
        user_voice = getattr(interaction.user, "voice", None)
        if (
            not voice_client
            or not user_voice
            or user_voice.channel != voice_client.channel
        ):
            await interaction.response.send_message(
                "يجب أن تكون في نفس الروم الصوتي مع البوت.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice_client = interaction.guild.voice_client
        current = now_playing.get(self.guild_id)
        if not voice_client or not current:
            await interaction.response.send_message(
                "ما فيه أغنية تعمل حاليًا.", ephemeral=True
            )
            return
        played = history.setdefault(self.guild_id, [])
        if played and played[-1] is current:
            played.pop()
        previous = played.pop() if played else None
        queue = queues.setdefault(self.guild_id, [])
        queue.insert(0, (current, interaction.channel))
        if previous:
            queue.insert(0, (previous, interaction.channel))
        skipped_guilds.add(self.guild_id)
        voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.secondary, row=0)
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice_client = interaction.guild.voice_client
        queues.pop(self.guild_id, None)
        repeat_guilds.discard(self.guild_id)
        queue_repeat_guilds.discard(self.guild_id)
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            skipped_guilds.add(self.guild_id)
            voice_client.stop()
        await interaction.response.send_message("⏹️ تم إيقاف التشغيل ومسح الطابور.")

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            button.emoji = "▶️"
        elif voice_client and voice_client.is_paused():
            voice_client.resume()
            button.emoji = "⏸️"
        else:
            await interaction.response.send_message(
                "ما فيه أغنية تعمل حاليًا.", ephemeral=True
            )
            return
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            skipped_guilds.add(self.guild_id)
            voice_client.stop()
            await interaction.response.defer()
        else:
            await interaction.response.send_message(
                "ما فيه أغنية لتخطيها.", ephemeral=True
            )

    async def _change_volume(
        self, interaction: discord.Interaction, delta: float
    ) -> None:
        voice_client = interaction.guild.voice_client
        current = volumes.get(self.guild_id, DEFAULT_VOLUME)
        volume = round(min(MAX_VOLUME, max(MIN_VOLUME, current + delta)), 2)
        volumes[self.guild_id] = volume
        if voice_client and isinstance(
            voice_client.source, discord.PCMVolumeTransformer
        ):
            voice_client.source.volume = volume
        self.embed.set_footer(text=f"🔊 مستوى الصوت: {round(volume * 100)}%")
        await interaction.response.edit_message(embed=self.embed, view=self)

    @discord.ui.button(
        label="التكرار متوقف",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def repeat_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild_id = self.guild_id
        if guild_id not in repeat_guilds and guild_id not in queue_repeat_guilds:
            repeat_guilds.add(guild_id)
            button.style = discord.ButtonStyle.success
            button.label = "تكرار الأغنية"
        elif guild_id in repeat_guilds:
            repeat_guilds.discard(guild_id)
            queue_repeat_guilds.add(guild_id)
            button.style = discord.ButtonStyle.primary
            button.label = "تكرار الطابور"
        else:
            queue_repeat_guilds.discard(guild_id)
            repeat_guilds.discard(guild_id)
            button.style = discord.ButtonStyle.secondary
            button.label = "التكرار متوقف"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = QueueView(self.guild_id)
        if view.children:
            await interaction.response.send_message(
                view.content(), view=view, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                view.content(), ephemeral=True
            )

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def volume_up_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._change_volume(interaction, VOLUME_STEP)


async def show_panel(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    track: Track,
    stream: dict[str, Any],
) -> None:
    await clear_panel(guild.id)
    volume = volumes.get(guild.id, DEFAULT_VOLUME)
    safe_title = track["title"].replace("[", "(").replace("]", ")")
    embed = discord.Embed(
        title="🎧 Now Playing",
        description=f"**[{safe_title}]({track['webpage']})**",
        color=0x5865F2,
    )
    if stream.get("thumbnail"):
        embed.set_thumbnail(url=stream["thumbnail"])
    if stream.get("uploader"):
        embed.add_field(name="👤 الفنان", value=str(stream["uploader"]), inline=True)
    duration = format_duration(stream.get("duration"))
    if duration:
        embed.add_field(name="⏱️ المدة", value=duration, inline=True)
    if track.get("requester"):
        embed.add_field(name="🙋 الطالب", value=track["requester"], inline=True)
    embed.set_footer(text=f"🔊 مستوى الصوت: {round(volume * 100)}%")
    try:
        panels[guild.id] = await channel.send(
            embed=embed, view=PlayerView(guild.id, embed)
        )
    except discord.DiscordException:
        logger.exception("Could not send the player panel")


async def play_next(guild: discord.Guild) -> None:
    lock = voice_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return
        if voice_client.is_playing() or voice_client.is_paused():
            return
        queue = queues.setdefault(guild.id, [])
        if not queue:
            now_playing.pop(guild.id, None)
            # Keep the last player panel and its controls visible while idle.
            return
        while queue:
            track, requester_channel = queue.pop(0)
            try:
                stream = await extract_info(track["webpage"])
                if not stream or not stream.get("url"):
                    raise ValueError("لم أتمكن من الحصول على رابط البث.")
                source = build_ffmpeg_source(
                    stream, volumes.get(guild.id, DEFAULT_VOLUME)
                )
                now_playing[guild.id] = track

                def after_play(
                    error: Exception | None,
                    _track: Track = track,
                    _channel: discord.abc.Messageable = requester_channel,
                ) -> None:
                    if error:
                        logger.error("Playback error: %s", error)
                    future = asyncio.run_coroutine_threadsafe(
                        advance_after_track(guild, _track, _channel, error is None),
                        bot.loop,
                    )

                    def report_failure(done: Any) -> None:
                        try:
                            done.result()
                        except Exception:
                            logger.exception("Could not start the next track")

                    future.add_done_callback(report_failure)

                voice_client.play(source, after=after_play)
                played = history.setdefault(guild.id, [])
                if not played or played[-1] is not track:
                    played.append(track)
                    del played[:-20]
                if last_announced.get(guild.id) is not track:
                    last_announced[guild.id] = track
                    await show_panel(guild, requester_channel, track, stream)
                return
            except Exception as playback_error:
                logger.exception("Could not play track: %s", track["title"])
                if now_playing.get(guild.id) is track:
                    now_playing.pop(guild.id, None)
                error_text = str(playback_error).lower()
                if "sign in to confirm you're not a bot" in error_text or "confirm you're not a bot" in error_text:
                    reason = "يوتيوب طلب تحققًا من البوت، وجربت أكثر من طريقة لاستخراج الصوت لكنها لم تنجح."
                elif "http error 403" in error_text or "status code 403" in error_text:
                    reason = "يوتيوب رفض الوصول إلى رابط البث. قد يكون التقييد من يوتيوب أو من عنوان استضافة البوت."
                else:
                    reason = "فشل استخراج الصوت من الرابط."
                await send_safely(
                    requester_channel,
                    f"تعذر تشغيل **{track['title']}**. {reason} سأنتقل للأغنية التالية.",
                )
        now_playing.pop(guild.id, None)
        await clear_panel(guild.id)


async def advance_after_track(
    guild: discord.Guild,
    finished_track: Track,
    requester_channel: discord.abc.Messageable,
    finished_ok: bool,
) -> None:
    # If YouTube playback fails, try one SoundCloud copy before moving on.
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

    if now_playing.get(guild.id) is finished_track:
        now_playing.pop(guild.id, None)
    skipped = guild.id in skipped_guilds
    skipped_guilds.discard(guild.id)
    if finished_ok and not skipped:
        if guild.id in repeat_guilds:
            queues.setdefault(guild.id, []).insert(0, (finished_track, requester_channel))
        elif guild.id in queue_repeat_guilds:
            queues.setdefault(guild.id, []).append((finished_track, requester_channel))
    await play_next(guild)


def user_shares_bot_voice_channel(ctx: commands.Context) -> bool:
    return bool(
        ctx.guild
        and ctx.voice_client
        and ctx.author.voice
        and ctx.author.voice.channel == ctx.voice_client.channel
    )


async def connect_voice(
    guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel
) -> discord.VoiceClient:
    lock = voice_connection_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        previous_target = voice_targets.get(guild.id)
        set_target(guild.id, channel.id)
        try:
            voice_client = guild.voice_client
            if voice_client and voice_client.is_connected():
                if voice_client.channel.id != channel.id:
                    await voice_client.move_to(channel, timeout=25.0)
            else:
                if voice_client:
                    await voice_client.disconnect(force=True)
                voice_client = await channel.connect(timeout=25.0, reconnect=True)
            return voice_client
        except Exception:
            set_target(guild.id, previous_target)
            raise


async def reconnect_to_voice(guild_id: int, channel_id: int) -> None:
    attempt = 0
    while voice_targets.get(guild_id) == channel_id:
        attempt += 1
        await asyncio.sleep(min(2 ** min(attempt, 6), 60))
        if voice_targets.get(guild_id) != channel_id:
            return
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            set_target(guild_id, None)
            return
        try:
            voice_client = guild.voice_client
            if (
                voice_client
                and voice_client.is_connected()
                and voice_client.channel.id == channel_id
            ):
                return
            await connect_voice(guild, channel)
            if queues.get(guild_id):
                await play_next(guild)
            return
        except Exception:
            logger.exception("Voice reconnection attempt %s failed", attempt)


def ensure_reconnect(guild_id: int) -> None:
    target_id = voice_targets.get(guild_id)
    if target_id is None:
        return
    current_task = voice_reconnect_tasks.get(guild_id)
    if current_task and not current_task.done():
        return
    task = asyncio.create_task(reconnect_to_voice(guild_id, target_id))
    voice_reconnect_tasks[guild_id] = task

    def clear_task(done: asyncio.Task[None]) -> None:
        if voice_reconnect_tasks.get(guild_id) is done:
            voice_reconnect_tasks.pop(guild_id, None)

    task.add_done_callback(clear_task)


@tasks.loop(seconds=30)
async def voice_watchdog() -> None:
    for guild_id, channel_id in list(voice_targets.items()):
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        voice_client = guild.voice_client
        if not (
            voice_client
            and voice_client.is_connected()
            and voice_client.channel.id == channel_id
        ):
            ensure_reconnect(guild_id)


@tasks.loop(seconds=30)
async def voice_stats_watchdog() -> None:
    now = time.time()
    for guild in bot.guilds:
        record = voice_stats.get(guild.id)
        if not record or not record.get("channel_id"):
            continue

        period_start = float(record.get("period_start", now))
        last_tick = float(record.get("last_tick", now))
        last_report = float(record.get("last_report", period_start))
        active_before = stats_active_channels.get(guild.id, active_human_voice_channels(guild))
        deadline = period_start + STATS_WEEK_SECONDS

        # Count activity only up to the end of the current 7-day period.
        tick_end = min(now, deadline)
        elapsed = max(0.0, tick_end - last_tick)
        seconds = record.setdefault("seconds", {})
        for voice_channel_id in active_before:
            key = str(voice_channel_id)
            seconds[key] = float(seconds.get(key, 0.0)) + elapsed

        if now >= deadline:
            # Publish the completed week's ranking, then start a clean 7-day period.
            await publish_weekly_stats(guild, record)
            record["seconds"] = {}
            record["period_start"] = now
            record["last_tick"] = now
            record["last_report"] = now
            stats_active_channels[guild.id] = active_human_voice_channels(guild)
        else:
            record["last_tick"] = now
            stats_active_channels[guild.id] = active_human_voice_channels(guild)

            # Refresh the top-three ranking every 10 minutes without resetting the weekly totals.
            if now - last_report >= STATS_UPDATE_SECONDS:
                await publish_weekly_stats(guild, record)
                record["last_report"] = now

    save_voice_stats()

@voice_stats_watchdog.before_loop
async def before_voice_stats_watchdog() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    logger.info("تم تسجيل الدخول باسم %s", bot.user)
    if not getattr(bot, "_role_panel_view_registered", False):
        bot.add_view(RolePanelView())
        bot._role_panel_view_registered = True
    try:
        synced = await bot.tree.sync()
        logger.info("تم مزامنة %s من أوامر السلاش", len(synced))
    except Exception:
        logger.exception("تعذرت مزامنة أوامر السلاش")
    # أعد تشغيل مراقبة الاتصال لضمان رجوع البوت للروم المحفوظ بعد الانقطاع أو إعادة التشغيل.
    if not voice_watchdog.is_running():
        voice_watchdog.start()

    # Start fresh activity sampling on reconnect so downtime is never counted.
    now = time.time()
    for guild in bot.guilds:
        record = voice_stats.get(guild.id)
        if record and record.get("channel_id"):
            record["last_tick"] = now
            stats_active_channels[guild.id] = active_human_voice_channels(guild)
    save_voice_stats()
    if not voice_stats_watchdog.is_running():
        voice_stats_watchdog.start()


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    # Track human joins/leaves immediately so brief visits are not missed by polling.
    guild_id = member.guild.id
    record = voice_stats.get(guild_id)
    if record and record.get("channel_id") and before.channel != after.channel:
        now = time.time()
        last_tick = float(record.get("last_tick", now))
        elapsed = max(0.0, now - last_tick)
        seconds = record.setdefault("seconds", {})
        for voice_channel_id in stats_active_channels.get(guild_id, set()):
            key = str(voice_channel_id)
            seconds[key] = float(seconds.get(key, 0.0)) + elapsed
        record["last_tick"] = now

        for channel in (before.channel, after.channel):
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                continue
            if any(not person.bot for person in channel.members):
                stats_active_channels.setdefault(guild_id, set()).add(channel.id)
            else:
                stats_active_channels.setdefault(guild_id, set()).discard(channel.id)
        save_voice_stats()

    # إذا كان الحدث يخص البوت نفسه، أعد الاتصال بالروم المحفوظ عند الانفصال أو النقل.
    # أمر !اخرج يمسح الهدف المحفوظ أولًا، لذلك لن يعيده البوت بعد استخدام الأمر.
    if bot.user and member.id == bot.user.id and before.channel != after.channel:
        ensure_reconnect(guild_id)


class RoleCreateModal(discord.ui.Modal, title="إنشاء رتبتك الخاصة"):
    role_name = discord.ui.TextInput(
        label="اسم الرتبة",
        placeholder="اكتب اسم الرتبة",
        min_length=1,
        max_length=100,
        required=True,
    )
    role_color = discord.ui.TextInput(
        label="لون الرتبة (HEX)",
        placeholder="#5865F2",
        min_length=6,
        max_length=7,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("هذا الزر يعمل داخل السيرفر فقط.", ephemeral=True)
            return

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "البوت يحتاج صلاحية **Manage Roles (إدارة الرتب)**.", ephemeral=True
            )
            return

        if member.id == guild.owner_id:
            await interaction.response.send_message(
                "ما أقدر أنشئ رتبة أعلى من مالك السيرفر.", ephemeral=True
            )
            return

        # الرتبة الجديدة تكون فوق أعلى رتبة مع العضو مباشرة،
        # إلا إذا كانت أعلى رتبة معه فيها Administrator فتكون تحتها مباشرة.
        top_role = member.top_role
        if top_role.permissions.administrator:
            # position = موقع رتبة الإدارة: الرتبة الجديدة تأخذ مكانها وتُدفع رتبة الإدارة للأعلى
            target_position = top_role.position
        else:
            target_position = top_role.position + 1

        if target_position < 1 or bot_member.top_role.position <= target_position:
            await interaction.response.send_message(
                "ما أقدر أرتب الرتبة بالمكان المطلوب. ارفع رتبة البوت فوق أعلى رتبة عندك، ثم جرّب مرة ثانية.",
                ephemeral=True,
            )
            return

        name = str(self.role_name.value).strip()
        color_text = str(self.role_color.value).strip().removeprefix("#")
        if len(color_text) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in color_text):
            await interaction.response.send_message(
                "اللون غير صحيح. اكتب لون HEX مثل `#5865F2`.", ephemeral=True
            )
            return
        if not name:
            await interaction.response.send_message("اكتب اسمًا للرتبة.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        new_role: discord.Role | None = None
        try:
            new_role = await guild.create_role(
                name=name,
                colour=discord.Colour(int(color_text, 16)),
                permissions=discord.Permissions.none(),
                mentionable=False,
                hoist=False,
                reason=f"Self-service role requested by {member} ({member.id})",
            )
            await new_role.edit(
                position=target_position,
                reason="Place self-service role according to member's Administrator role hierarchy",
            )
            await member.add_roles(new_role, reason="Self-service role creation")

            guild_owners = created_roles.setdefault(guild.id, {})
            previous_id = guild_owners.get(member.id)
            if previous_id and previous_id != new_role.id:
                previous_role = guild.get_role(previous_id)
                if previous_role is not None:
                    try:
                        await member.remove_roles(previous_role, reason="Replacing previous self-service role")
                    except discord.DiscordException:
                        logger.warning("Could not remove previous self-service role %s", previous_role.id)
                    # Never delete an Administrator role or a managed/integration role.
                    if not previous_role.managed and not previous_role.permissions.administrator:
                        try:
                            await previous_role.delete(reason="Replaced by a newer self-service role")
                        except discord.DiscordException:
                            logger.warning("Could not delete previous self-service role %s", previous_role.id)
            guild_owners[member.id] = new_role.id
            save_created_roles()
            await interaction.followup.send(
                f"تم إنشاء {new_role.mention} وإعطاؤها لك. إذا أنشأت رتبة جديدة لاحقًا، ستُستبدل رتبتك السابقة.",
                ephemeral=True,
            )
        except discord.Forbidden:
            if new_role is not None:
                try:
                    await new_role.delete(reason="Cleanup after failed self-service role creation")
                except discord.DiscordException:
                    pass
            await interaction.followup.send(
                "ما قدرت أنشئ الرتبة أو أرتبها. تأكد أن رتبة البوت أعلى من رتبتك وأعلى من الرتب التي يحاول إدارتها.",
                ephemeral=True,
            )
        except discord.DiscordException:
            logger.exception("Failed to create self-service role for %s", member.id)
            if new_role is not None and new_role not in member.roles:
                try:
                    await new_role.delete(reason="Cleanup after failed self-service role creation")
                except discord.DiscordException:
                    pass
            await interaction.followup.send("صار خطأ أثناء إنشاء الرتبة. تأكد من صلاحيات البوت وحاول مرة ثانية.", ephemeral=True)


class RolePanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="إنشاء رتبة",
        style=discord.ButtonStyle.primary,
        custom_id="self_service_role:create",
    )
    async def create_role_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(RoleCreateModal())


@bot.tree.command(name="setup_role", description="إعداد لوحة إنشاء الرتب في هذه القناة")
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
async def setup_role_panel(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("هذا الأمر داخل السيرفر فقط.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "تحتاج صلاحية إدارة السيرفر لاستخدام هذا الأمر.", ephemeral=True
        )
        return
    embed = discord.Embed(
        title="إنشاء رتبة خاصة",
        description="اضغط الزر واكتب اسم رتبتك ولونها مثل `#5865F2`.",
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, view=RolePanelView())


@bot.tree.command(name="setup", description="تفعيل إحصائيات الرومات الصوتية في هذه القناة")
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
async def setup_voice_stats(interaction: discord.Interaction) -> None:
    """Set the text channel where weekly voice activity rankings are posted."""
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("اكتب الأمر داخل شات نصي في السيرفر.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "تحتاج صلاحية **إدارة السيرفر (Manage Server)** لاستخدام هذا الأمر.",
            ephemeral=True,
        )
        return

    now = time.time()
    record = voice_stats.get(interaction.guild.id)
    if not record or not record.get("channel_id"):
        record = {
            "channel_id": interaction.channel.id,
            "period_start": now,
            "last_tick": now,
            "last_report": now,
            "message_ids": [],
            "seconds": {},
        }
        voice_stats[interaction.guild.id] = record
    else:
        # Changing the report channel keeps the current week's accumulated totals.
        record["channel_id"] = interaction.channel.id
        record["last_tick"] = now

    stats_active_channels[interaction.guild.id] = active_human_voice_channels(interaction.guild)
    save_voice_stats()
    embed = discord.Embed(
        title="تم تفعيل إحصائيات الرومات الصوتية",
        description=(
            "بدأ حساب مدة وجود الأعضاء في جميع الرومات الصوتية، حتى لو كانوا صامتين.\n"
            "سيتم تحديث ترتيب الرومات كل 10 دقائق، وتصفير الإحصائيات وبدء أسبوع جديد كل 7 أيام في هذا الشات."
        ),
        color=0x57F287,
    )
    embed.add_field(name="تحديث القائمة", value="كل 10 دقائق", inline=True)
    embed.add_field(name="تصفير الإحصائيات", value="كل 7 أيام", inline=True)
    embed.add_field(name="طريقة الحساب", value="وجود عضو واحد على الأقل في الروم", inline=True)
    await interaction.response.send_message(embed=embed)




@bot.tree.command(name="حذف", description="حذف رسائل حسب العدد أو العضو أو الروم")
@discord.app_commands.guild_only()
@discord.app_commands.describe(
    العدد="عدد الرسائل (اختياري، من 1 إلى 100). إذا تركته مع العضو أو الروم فسيحذف كل الرسائل المطابقة",
    العضو="اختياري: حذف رسائل هذا العضو فقط",
    الروم="اختياري: الروم الذي تريد الحذف منه، وإلا فالروم الحالي",
)
@discord.app_commands.default_permissions(manage_messages=True)
async def delete_messages(
    interaction: discord.Interaction,
    العدد: discord.app_commands.Range[int, 1, 100] | None = None,
    العضو: discord.Member | None = None,
    الروم: discord.TextChannel | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("هذا الأمر يعمل داخل السيرفر فقط.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "تحتاج صلاحية **إدارة الرسائل (Manage Messages)** لاستخدام الأمر.",
            ephemeral=True,
        )
        return

    target = الروم or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("حدد رومًا نصيًا صالحًا.", ephemeral=True)
        return

    bot_member = guild.me
    permissions = target.permissions_for(bot_member) if bot_member else None
    if not permissions or not permissions.view_channel or not permissions.read_message_history or not permissions.manage_messages:
        await interaction.response.send_message(
            f"أحتاج في {target.mention} صلاحيات عرض الروم وقراءة سجل الرسائل وإدارة الرسائل.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # إذا لم يحدد المستخدم العدد، فوجود عضو أو روم يعني حذف كل الرسائل المطابقة.
        # أما /حذف بلا أي خيارات فيحذف آخر 100 رسالة فقط لتجنب حذف الروم كاملًا بالخطأ.
        limit = العدد if العدد is not None else (100 if العضو is None and الروم is None else None)
        matches: list[discord.Message] = []
        async for message in target.history(limit=None):
            if العضو is None or message.author.id == العضو.id:
                matches.append(message)
                if limit is not None and len(matches) >= limit:
                    break

        if not matches:
            await interaction.followup.send("ما لقيت رسائل مطابقة للحذف.", ephemeral=True)
            return

        # Discord لا يسمح بالحذف الجماعي للرسائل الأقدم من 14 يومًا.
        now = discord.utils.utcnow()
        recent = [m for m in matches if (now - m.created_at).days < 14]
        old = [m for m in matches if (now - m.created_at).days >= 14]

        deleted_count = 0
        if recent:
            for start in range(0, len(recent), 100):
                batch = recent[start:start + 100]
                if len(batch) == 1:
                    await batch[0].delete()
                else:
                    await target.delete_messages(batch)
                deleted_count += len(batch)
        for message in old:
            await message.delete()
            deleted_count += 1

        who = f" للعضو {العضو.mention}" if العضو else ""
        await interaction.followup.send(
            f"تم حذف **{deleted_count}** رسالة من {target.mention}{who}.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "ما قدرت أحذف الرسائل. تأكد أن رتبة البوت تسمح له بإدارة الرسائل في هذا الروم.",
            ephemeral=True,
        )
    except discord.HTTPException:
        logger.exception("Message deletion failed")
        await interaction.followup.send(
            "صار خطأ أثناء الحذف؛ قد تكون بعض الرسائل حُذفت بالفعل. تحقق من صلاحيات البوت وحاول مرة ثانية.",
            ephemeral=True,
        )



@bot.command(name="ادخل")
@commands.guild_only()
async def join_voice(ctx: commands.Context) -> None:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("ادخل رومًا صوتيًا أولًا ثم اكتب `!ادخل`.")
        return
    channel = ctx.author.voice.channel
    permissions = channel.permissions_for(ctx.guild.me)
    if not permissions.view_channel or not permissions.connect:
        await ctx.send(
            "ما أقدر أدخل هذا الروم. اطلب من مسؤول السيرفر السماح للبوت "
            "بصلاحية **View Channel** و **Connect**."
        )
        return
    await ctx.send(f"⏳ أحاول دخول الروم الصوتي **{channel.name}**...")
    try:
        await connect_voice(ctx.guild, channel)
    except Exception:
        logger.exception("Unexpected error while joining the voice channel")
        await ctx.send("تعذر دخول الروم بسبب خطأ غير متوقع.")
        return
    await ctx.send(f"دخلت الروم الصوتي: **{channel.name}**")


@bot.command(name="ش")
@commands.guild_only()
async def play_song(ctx: commands.Context, *, search: str | None = None) -> None:
    if not search:
        await ctx.send("اكتب اسم الأغنية بعد الأمر، مثل: `ش اسم الأغنية`")
        return
    if "spotify.com" in search:
        await ctx.send(
            "روابط سبوتيفاي غير مدعومة مباشرة. اكتب اسم الأغنية وأنا أبحث عنها."
        )
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("ادخل رومًا صوتيًا أولًا.")
        return
    if len(queues.get(ctx.guild.id, [])) >= MAX_QUEUE:
        await ctx.send(f"الطابور ممتلئ (الحد الأقصى {MAX_QUEUE} أغنية).")
        return
    channel = ctx.author.voice.channel
    try:
        voice_client = await connect_voice(ctx.guild, channel)
    except Exception:
        logger.exception("Could not join the voice channel")
        await ctx.send("تعذر دخول الروم الصوتي.")
        return
    await ctx.send(f"أبحث عن **{search}**...")
    try:
        track = await find_track(search)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    except Exception:
        logger.exception("Search crashed")
        await ctx.send("صار خطأ أثناء البحث، جرّب مرة ثانية.")
        return
    track["requester"] = ctx.author.display_name
    queues.setdefault(ctx.guild.id, []).append((track, ctx.channel))
    if voice_client.is_playing() or voice_client.is_paused():
        await ctx.send(f"أُضيفت إلى الطابور: **{track['title']}**")
    else:
        await play_next(ctx.guild)


@bot.command(name="وقف")
@commands.guild_only()
async def pause_song(ctx: commands.Context) -> None:
    voice_client = ctx.voice_client
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
    elif voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.send("تم إيقاف الأغنية مؤقتًا.")
    else:
        await ctx.send("ما فيه أغنية تعمل حاليًا.")


@bot.command(name="ك", aliases=["كمل"])
@commands.guild_only()
async def resume_song(ctx: commands.Context) -> None:
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
    elif ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("تم استكمال الأغنية.")
    else:
        await ctx.send("ما فيه أغنية متوقفة مؤقتًا.")


@bot.command(name="س", aliases=["سكب"])
@commands.guild_only()
async def skip_song(ctx: commands.Context) -> None:
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
    elif ctx.voice_client and (
        ctx.voice_client.is_playing() or ctx.voice_client.is_paused()
    ):
        skipped_guilds.add(ctx.guild.id)
        ctx.voice_client.stop()
        await ctx.send("تم تخطي الأغنية.")
    else:
        await ctx.send("ما فيه أغنية لتخطيها.")


@bot.command(name="كرر")
@commands.guild_only()
async def toggle_repeat(
    ctx: commands.Context, mode: str | None = None
) -> None:
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
        return
    guild_id = ctx.guild.id
    if mode and mode.lower() in {"ط", "طابور"}:
        if guild_id in queue_repeat_guilds:
            queue_repeat_guilds.discard(guild_id)
            await ctx.send("تم إيقاف تكرار الطابور.")
        else:
            queue_repeat_guilds.add(guild_id)
            repeat_guilds.discard(guild_id)
            await ctx.send("تم تفعيل تكرار الطابور كاملًا.")
        return
    if guild_id in repeat_guilds:
        repeat_guilds.discard(guild_id)
        await ctx.send("تم إيقاف تكرار الأغنية الحالية.")
    else:
        repeat_guilds.add(guild_id)
        queue_repeat_guilds.discard(guild_id)
        await ctx.send("تم تفعيل تكرار الأغنية الحالية.")


@bot.command(name="طابور")
@commands.guild_only()
async def show_queue(ctx: commands.Context) -> None:
    await ctx.send(queue_text(ctx.guild.id))


@bot.command(name="اخرج")
@commands.guild_only()
async def leave_voice(ctx: commands.Context) -> None:
    guild_id = ctx.guild.id
    set_target(guild_id, None)
    reconnect_task = voice_reconnect_tasks.pop(guild_id, None)
    if reconnect_task and not reconnect_task.done():
        reconnect_task.cancel()
    if not ctx.voice_client:
        await ctx.send("أنا لست في روم صوتي.")
        return
    queues.pop(guild_id, None)
    now_playing.pop(guild_id, None)
    repeat_guilds.discard(guild_id)
    queue_repeat_guilds.discard(guild_id)
    skipped_guilds.discard(guild_id)
    last_announced.pop(guild_id, None)
    history.pop(guild_id, None)
    await clear_panel(guild_id)
    await ctx.voice_client.disconnect(force=True)
    await ctx.send("خرجت من الروم الصوتي.")


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("هذه الأوامر تعمل داخل السيرفر فقط.")
    elif isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("تحتاج صلاحية **إدارة السيرفر (Manage Server)** لاستخدام هذا الأمر.")
    elif isinstance(error, commands.CommandInvokeError):
        logger.error("Command %s failed", ctx.command, exc_info=error.original)
    else:
        logger.warning("Command error: %s", error)


def ensure_single_instance(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError:
        raise SystemExit(
            f"المنفذ {port} مشغول: فيه نسخة ثانية من البوت تعمل. "
            "أوقفها أولًا (Stop) ثم شغّل نسخة واحدة فقط."
        )
    finally:
        probe.close()


def keep_alive() -> None:
    app = Flask(__name__)

    @app.route("/")
    def home() -> str:
        return "I'm alive"

    port = int(os.getenv("PORT", "8080"))
    ensure_single_instance(port)
    Thread(
        target=lambda: app.run(host="0.0.0.0", port=port),
        daemon=True,
    ).start()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("أضف توكن البوت في Secrets باسم DISCORD_TOKEN.")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg غير موجود.")
    keep_alive()
    bot.run(TOKEN, log_handler=None)
