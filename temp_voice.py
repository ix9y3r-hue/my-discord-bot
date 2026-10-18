"""Temporary voice channels extension for discord.py 2.x.

Install by adding `await bot.load_extension("temp_voice")` to the bot's setup_hook.
The /setup_tempvoice command creates a generator voice channel; members join it to
receive a private temporary voice channel, which is deleted when empty.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)
CONFIG_FILE = Path(os.getenv("TEMP_VOICE_CONFIG_FILE", "temp_voice_config.json"))


def _load_config() -> dict[int, dict[str, int]]:
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        return {
            int(guild_id): {
                key: int(value)
                for key, value in data.items()
                if key in {"generator_channel_id", "category_id"} and value
            }
            for guild_id, data in raw.items()
            if isinstance(data, dict)
        }
    except (OSError, ValueError, TypeError):
        return {}


def _save_config(config: dict[int, dict[str, int]]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            {str(guild_id): data for guild_id, data in config.items()},
            file,
            ensure_ascii=False,
            indent=2,
        )
    temporary.replace(CONFIG_FILE)


class TemporaryVoice(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: dict[int, dict[str, int]] = _load_config()
        # IDs of channels created by this feature; rebuilt from the saved category
        # and the stable channel-name prefix after a restart.
        self.temporary_channel_ids: set[int] = set()
        self._creating_for: set[int] = set()

    @app_commands.command(
        name="setup_tempvoice",
        description="إنشاء نظام الرومات الصوتية المؤقتة",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(category="الفئة التي ستظهر فيها الرومات المؤقتة (اختياري)")
    async def setup_tempvoice(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر يعمل داخل السيرفر فقط.", ephemeral=True
            )
            return

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "أحتاج صلاحية **إدارة القنوات (Manage Channels)** لإنشاء الرومات.",
                ephemeral=True,
            )
            return

        if category is None and isinstance(interaction.channel, discord.abc.GuildChannel):
            category = interaction.channel.category

        # Create the generator channel in the chosen category.
        try:
            existing = self.config.get(guild.id, {})
            existing_id = existing.get("generator_channel_id")
            old_generator = guild.get_channel(existing_id) if existing_id else None
            if isinstance(old_generator, discord.VoiceChannel):
                await old_generator.delete(reason="إعادة إعداد الرومات الصوتية المؤقتة")

            generator = await guild.create_voice_channel(
                name="➕・ادخل لإنشاء روم",
                category=category,
                reason=f"إعداد الرومات المؤقتة بواسطة {interaction.user}",
            )
            self.config[guild.id] = {
                "generator_channel_id": generator.id,
                "category_id": category.id if category else 0,
            }
            _save_config(self.config)
        except discord.Forbidden:
            await interaction.response.send_message(
                "ما عندي صلاحيات كافية لإنشاء القناة. أعطني **Manage Channels**.",
                ephemeral=True,
            )
            return
        except discord.DiscordException:
            logger.exception("Failed setting up temporary voice channels")
            await interaction.response.send_message(
                "صار خطأ أثناء إعداد النظام. تأكد من صلاحيات البوت وحاول مرة ثانية.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="نظام الرومات الصوتية المؤقتة",
            description=(
                f"ادخل إلى {generator.mention} وسيُنشأ لك روم صوتي خاص تلقائيًا.\n\n"
                "- تنتقل إلى الروم الجديد مباشرة.\n"
                "- تقدر تغيّر اسم الروم وتتحكم بمن يدخل إليه.\n"
                "- يُحذف الروم تلقائيًا عندما يصبح فارغًا."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="إعداد النظام مرة واحدة فقط")
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Rebuild our temporary-channel registry after bot restarts.
        for guild in self.bot.guilds:
            settings = self.config.get(guild.id, {})
            category_id = settings.get("category_id")
            category = guild.get_channel(category_id) if category_id else None
            if isinstance(category, discord.CategoryChannel):
                for channel in category.voice_channels:
                    if channel.name.startswith("🔊・"):
                        self.temporary_channel_ids.add(channel.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or member.guild is None:
            return

        guild = member.guild
        settings = self.config.get(guild.id, {})
        generator_id = settings.get("generator_channel_id")
        category_id = settings.get("category_id")

        # Create a channel when a member joins the generator channel.
        if after.channel is not None and after.channel.id == generator_id:
            if member.id in self._creating_for:
                return
            self._creating_for.add(member.id)
            try:
                bot_member = guild.me
                if bot_member is None or not bot_member.guild_permissions.manage_channels:
                    logger.warning("Missing Manage Channels permission in guild %s", guild.id)
                    return

                category = guild.get_channel(category_id) if category_id else after.channel.category
                if not isinstance(category, discord.CategoryChannel):
                    category = after.channel.category

                overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=False,
                    ),
                    member: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        manage_channels=True,
                        move_members=True,
                        mute_members=True,
                        deafen_members=True,
                    ),
                }
                temp_channel = await guild.create_voice_channel(
                    name=f"🔊・{member.display_name}",
                    category=category,
                    overwrites=overwrites,
                    reason=f"روم مؤقت للعضو {member} ({member.id})",
                )
                self.temporary_channel_ids.add(temp_channel.id)
                await member.move_to(temp_channel, reason="نقل العضو إلى رومه المؤقت")
            except discord.Forbidden:
                logger.warning("Insufficient permissions to create/move temporary voice channel")
            except discord.HTTPException:
                logger.exception("Could not create temporary voice channel")
            finally:
                self._creating_for.discard(member.id)

        # Remove an empty temporary channel after the last member leaves.
        if before.channel is not None and before.channel.id in self.temporary_channel_ids:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete(reason="حذف الروم المؤقت لأنه أصبح فارغًا")
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.warning("Cannot delete empty temporary channel %s", before.channel.id)
                except discord.HTTPException:
                    logger.exception("Failed deleting temporary channel %s", before.channel.id)
                finally:
                    self.temporary_channel_ids.discard(before.channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TemporaryVoice(bot))
