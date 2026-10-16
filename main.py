import asyncio
import json
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
MAX_QUEUE = 50
EXTRACT_TIMEOUT = 45  # ثواني

# أوامر مختصرة تشتغل بدون "!"
SHORT_COMMANDS = {"ش", "وقف", "ك", "كمل", "س", "سكب", "كرر"}

Track = dict[str, str]


def get_command_prefix(_bot: commands.Bot, message: discord.Message) -> str:
    parts = message.content.strip().split(maxsplit=1)
    if parts and parts[0] in SHORT_COMMANDS:
        return ""
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
bot = commands.Bot(command_prefix=get_command_prefix, intents=intents)

BASE_YTDL = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
}
# البحث السريع: ما نجيب كل الصيغ، بس العنوان والرابط
ytdl_search = yt_dlp.YoutubeDL({**BASE_YTDL, "extract_flat": "in_playlist"})
# جلب رابط البث الفعلي وقت التشغيل
ytdl_stream = yt_dlp.YoutubeDL(BASE_YTDL)
ytdl_lock = asyncio.Lock()

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS_AFTER = "-vn"

queues: dict[int, list[tuple[Track, discord.abc.Messageable]]] = {}
now_playing: dict[int, Track] = {}
repeat_guilds: set[int] = set()  # السيرفرات اللي فعّلت التكرار
skipped_guilds: set[int] = set()  # تخطي يدوي: ما نكرر الأغنية
last_announced: dict[int, Track] = {}
history: dict[int, list[Track]] = {}  # الأغاني اللي اشتغلت (لزر السابق)
volumes: dict[int, float] = {}  # مستوى الصوت لكل سيرفر
panels: dict[int, discord.Message] = {}  # لوحة التحكم الحالية لكل سيرفر
DEFAULT_VOLUME = 0.5
MIN_VOLUME = 0.1
MAX_VOLUME = 1.5
VOLUME_STEP = 0.1
voice_locks: dict[int, asyncio.Lock] = {}
voice_connection_locks: dict[int, asyncio.Lock] = {}
STATE_FILE = os.getenv("STATE_FILE", "voice_state.json")


def load_targets() -> dict[int, int]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


# الروم الصوتي المطلوب بقاء البوت فيه لكل سيرفر (يُحفظ في ملف ليرجع بعد إعادة التشغيل)
voice_targets: dict[int, int] = load_targets()


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


# ───────────────────────── البحث والاستخراج ─────────────────────────


async def extract_info(
    query: str, *, flat: bool = False
) -> dict[str, Any] | None:
    ydl = ytdl_search if flat else ytdl_stream
    async with ytdl_lock:
        return await asyncio.wait_for(
            asyncio.to_thread(ydl.extract_info, query, download=False),
            timeout=EXTRACT_TIMEOUT,
        )


def build_queries(search: str) -> list[str]:
    if search.startswith(("http://", "https://")):
        return [search]
    # أول شي SoundCloud، وإذا ما لقى نجرب يوتيوب
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
        return {
            "title": data.get("title") or "أغنية بدون اسم",
            "webpage": webpage,
        }

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
        stream["url"],
        before_options=before,
        options=FFMPEG_OPTIONS_AFTER,
    )
    return discord.PCMVolumeTransformer(source, volume=volume)


# ───────────────────────── التشغيل ─────────────────────────


# ───────────────────────── لوحة التحكم ─────────────────────────


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


class PlayerView(discord.ui.View):
    def __init__(self, guild_id: int, embed: discord.Embed) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.embed = embed
        self.repeat_button.style = (
            discord.ButtonStyle.success
            if guild_id in repeat_guilds
            else discord.ButtonStyle.secondary
        )

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
        if voice_client and isinstance(voice_client.source, discord.PCMVolumeTransformer):
            voice_client.source.volume = volume
        self.embed.set_footer(text=f"🔊 مستوى الصوت: {round(volume * 100)}%")
        await interaction.response.edit_message(embed=self.embed, view=self)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def volume_down_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._change_volume(interaction, -VOLUME_STEP)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def repeat_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.guild_id in repeat_guilds:
            repeat_guilds.discard(self.guild_id)
            button.style = discord.ButtonStyle.secondary
        else:
            repeat_guilds.add(self.guild_id)
            button.style = discord.ButtonStyle.success
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            queue_text(self.guild_id), ephemeral=True
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
        # إذا فيه أغنية تشتغل لا نلمس الطابور (تجنب "Already playing")
        if voice_client.is_playing() or voice_client.is_paused():
            return

        queue = queues.setdefault(guild.id, [])
        if not queue:
            now_playing.pop(guild.id, None)
            await clear_panel(guild.id)
            return

        while queue:
            track, requester_channel = queue.pop(0)
            try:
                stream = await extract_info(track["webpage"])
                if not stream or not stream.get("url"):
                    raise ValueError("لم أتمكن من الحصول على رابط البث.")

                source = build_ffmpeg_source(stream, volumes.get(guild.id, DEFAULT_VOLUME))
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
                # ما نكرر رسالة "الآن تعمل" عند تكرار نفس الأغنية
                played = history.setdefault(guild.id, [])
                if not played or played[-1] is not track:
                    played.append(track)
                    del played[:-20]
                if last_announced.get(guild.id) is not track:
                    last_announced[guild.id] = track
                    await show_panel(guild, requester_channel, track, stream)
                return
            except Exception:
                logger.exception("Could not play track: %s", track["title"])
                if now_playing.get(guild.id) is track:
                    now_playing.pop(guild.id, None)
                await send_safely(
                    requester_channel,
                    f"تعذر تشغيل **{track['title']}**؛ سأنتقل للأغنية التالية.",
                )

        now_playing.pop(guild.id, None)
        await clear_panel(guild.id)


async def advance_after_track(
    guild: discord.Guild,
    finished_track: Track,
    requester_channel: discord.abc.Messageable,
    finished_ok: bool,
) -> None:
    if now_playing.get(guild.id) is finished_track:
        now_playing.pop(guild.id, None)

    skipped = guild.id in skipped_guilds
    skipped_guilds.discard(guild.id)
    # التكرار: نرجّع الأغنية أول الطابور إذا انتهت طبيعيًا (مو تخطي ولا خطأ)
    if guild.id in repeat_guilds and finished_ok and not skipped:
        queues.setdefault(guild.id, []).insert(0, (finished_track, requester_channel))

    await play_next(guild)


# ───────────────────────── الاتصال الصوتي ─────────────────────────


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
    """يدخل الروم أو ينتقل له، مع قفل يمنع تضارب الاتصالات."""
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
    """يعيد المحاولة باستمرار (مع تأخير متزايد) عشان البوت يبقى 24/7."""
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
    """يشغّل مهمة إعادة الاتصال إذا ما فيه مهمة شغالة."""
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
    """كل 30 ثانية يتأكد أن البوت في الروم المطلوب، وإذا لا يرجّعه."""
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


@bot.event
async def on_ready() -> None:
    logger.info("تم تسجيل الدخول باسم %s", bot.user)

    # اختياري: روم افتراضي من متغير بيئة VOICE_CHANNEL_ID (يفيد بعد إعادة النشر على Render)
    env_channel = os.getenv("VOICE_CHANNEL_ID")
    if env_channel and env_channel.isdigit():
        channel = bot.get_channel(int(env_channel))
        if (
            isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
            and channel.guild.id not in voice_targets
        ):
            set_target(channel.guild.id, channel.id)

    if not voice_watchdog.is_running():
        voice_watchdog.start()


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if not bot.user or member.id != bot.user.id or before.channel == after.channel:
        return

    guild_id = member.guild.id
    target_id = voice_targets.get(guild_id)
    if target_id is None or (after.channel and after.channel.id == target_id):
        return

    ensure_reconnect(guild_id)


# ───────────────────────── الأوامر ─────────────────────────


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

    await ctx.send(f"✅ دخلت الروم الصوتي: **{channel.name}**")


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

    await ctx.send(f"🔎 أبحث عن **{search}**...")
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
        await ctx.send(f"➕ أُضيفت إلى الطابور: **{track['title']}**")
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
        await ctx.send("⏸️ تم إيقاف الأغنية مؤقتًا.")
    else:
        await ctx.send("ما فيه أغنية تعمل حاليًا.")


@bot.command(name="ك", aliases=["كمل"])
@commands.guild_only()
async def resume_song(ctx: commands.Context) -> None:
    voice_client = ctx.voice_client
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
    elif voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.send("▶️ تم استكمال الأغنية.")
    else:
        await ctx.send("ما فيه أغنية متوقفة مؤقتًا.")


@bot.command(name="س", aliases=["سكب"])
@commands.guild_only()
async def skip_song(ctx: commands.Context) -> None:
    voice_client = ctx.voice_client
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
    elif voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        skipped_guilds.add(ctx.guild.id)
        voice_client.stop()
        await ctx.send("⏭️ تم تخطي الأغنية.")
    else:
        await ctx.send("ما فيه أغنية لتخطيها.")


@bot.command(name="كرر")
@commands.guild_only()
async def toggle_repeat(ctx: commands.Context) -> None:
    if not user_shares_bot_voice_channel(ctx):
        await ctx.send("يجب أن تكون في نفس الروم الصوتي مع البوت.")
    elif ctx.guild.id in repeat_guilds:
        repeat_guilds.discard(ctx.guild.id)
        await ctx.send("🔁 تم إيقاف التكرار.")
    else:
        repeat_guilds.add(ctx.guild.id)
        await ctx.send("🔁 تم تفعيل التكرار: الأغنية الحالية تعيد نفسها بعد ما تنتهي.")


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

    # نفرغ الطابور قبل الخروج عشان ما يبدأ البوت أغنية جديدة
    queues.pop(guild_id, None)
    now_playing.pop(guild_id, None)
    repeat_guilds.discard(guild_id)
    skipped_guilds.discard(guild_id)
    last_announced.pop(guild_id, None)
    history.pop(guild_id, None)
    await clear_panel(guild_id)
    await ctx.voice_client.disconnect(force=True)
    await ctx.send("👋 خرجت من الروم الصوتي.")


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("هذه الأوامر تعمل داخل السيرفر فقط.")
    elif isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.CommandInvokeError):
        logger.error("Command %s failed", ctx.command, exc_info=error.original)
    else:
        logger.warning("Command error: %s", error)


# ───────────────────────── التشغيل ─────────────────────────


def ensure_single_instance(port: int) -> None:
    """إذا المنفذ مشغول معناه فيه نسخة ثانية من البوت تعمل، فنخرج."""
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
