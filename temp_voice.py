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


    @app_commands.command(
        name="setup_voice_panel",
        description="إرسال لوحة التحكم بالرومات الصوتية المؤقتة",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_voice_panel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="لوحة التحكم بالرومات الصوتية المؤقتة",
            description=(
                "ادخل رومك الصوتي المؤقت أولًا، ثم استخدم الأزرار التالية:\n"
                "• تغيير اسم الروم\n"
                "• قفل الروم أو فتحه\n"
                "• تحديد عدد الأعضاء\n"
                "• حذف الروم المؤقت\n\n"
                "التحكم متاح لمالك الروم المؤقت فقط."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="لوحة الرومات المؤقتة")
        await interaction.response.send_message(embed=embed, view=TempVoicePanel())

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Rebuild our temporary-channel registry after bot restarts.
        for guild in self.bot.guilds:
            settings = self.config.get(guild.id, {})
            category_id = settings.get("category_id")
            category = guild.get_channel(category_id) if category_id else None
            if isinstance(category, discord.CategoryChannel):
                candidates = category.voice_channels
            else:
                # The category is optional; recover temporary rooms across the guild after restart.
                candidates = guild.voice_channels
            for channel in candidates:
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



def _get_owned_temp_channel(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None:
        return None
    channel = member.voice.channel
    if not isinstance(channel, discord.VoiceChannel) or channel.id not in _all_temp_ids(interaction.client):
        return None
    if channel.overwrites_for(member).manage_channels is True:
        return channel
    return None


def _all_temp_ids(bot: commands.Bot) -> set[int]:
    cog = bot.get_cog("TemporaryVoice")
    return cog.temporary_channel_ids if cog else set()


def _owner(channel: discord.VoiceChannel) -> discord.Member | None:
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member) and overwrite.manage_channels is True:
            return target
    return None


async def _reply(interaction: discord.Interaction, message: str, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


async def _room_or_reply(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    channel = _get_owned_temp_channel(interaction)
    if channel is None:
        await _reply(interaction, "ادخل رومك الصوتي المؤقت الذي تملكه أولًا.")
    return channel


class ArabicRoomModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, custom_id: str, *, default: str = "", max_length: int = 90):
        super().__init__(title=title, timeout=180)
        self.field = discord.ui.TextInput(label=label, custom_id=custom_id, default=default or None, max_length=max_length, required=True)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await _room_or_reply(interaction)
        if channel is None:
            return
        value = str(self.field.value).strip()
        try:
            if self.field.custom_id == "room_name":
                await channel.edit(name=f"🔊・{value[:90]}")
                msg = "تم تغيير اسم الروم."
            elif self.field.custom_id == "room_limit":
                amount = int(value)
                if not 0 <= amount <= 99:
                    raise ValueError
                await channel.edit(user_limit=amount)
                msg = "تم تحديث حد الأعضاء. الصفر يعني بلا حد."
            elif self.field.custom_id == "room_bitrate":
                amount = int(value)
                if not 8 <= amount <= 384:
                    raise ValueError
                await channel.edit(bitrate=amount * 1000)
                msg = "تم تحديث جودة الصوت."
            elif self.field.custom_id == "room_status":
                await channel.edit(name=f"🔊・{value[:90]}")
                msg = "تم تحديث حالة الروم في اسمه."
            elif self.field.custom_id in {"room_trust", "room_untrust", "room_invite", "room_uninvite", "room_kick", "room_block", "room_unblock", "room_transfer"}:
                try:
                    user_id = int(value.replace("<@", "").replace("!", "").replace(">", ""))
                except ValueError:
                    await _reply(interaction, "اكتب آيدي العضو بالأرقام فقط.")
                    return
                member = channel.guild.get_member(user_id)
                if member is None:
                    try:
                        member = await channel.guild.fetch_member(user_id)
                    except discord.HTTPException:
                        await _reply(interaction, "ما لقيت العضو في السيرفر.")
                        return
                overwrite = channel.overwrites_for(member)
                action = self.field.custom_id
                if action in {"room_trust", "room_invite"}:
                    overwrite.view_channel = True
                    overwrite.connect = True
                    await channel.set_permissions(member, overwrite=overwrite, reason="إدارة الروم المؤقت")
                    msg = "تم السماح للعضو بالدخول."
                elif action == "room_untrust":
                    overwrite.connect = False
                    await channel.set_permissions(member, overwrite=overwrite, reason="إلغاء الثقة في الروم")
                    msg = "تم منع العضو من الدخول."
                elif action == "room_uninvite":
                    await channel.set_permissions(member, overwrite=None, reason="إلغاء دعوة الروم")
                    msg = "تم إلغاء صلاحيات العضو الخاصة."
                elif action == "room_kick":
                    if member.voice and member.voice.channel == channel:
                        await member.move_to(None, reason="إخراج عضو من روم مؤقت")
                    msg = "تم إخراج العضو إذا كان داخل الروم."
                elif action == "room_block":
                    overwrite.view_channel = False
                    overwrite.connect = False
                    await channel.set_permissions(member, overwrite=overwrite, reason="حظر عضو من الروم")
                    msg = "تم حظر العضو من الروم."
                elif action == "room_unblock":
                    await channel.set_permissions(member, overwrite=None, reason="رفع الحظر عن الروم")
                    msg = "تم رفع الحظر الخاص عن العضو."
                else:
                    current = _owner(channel)
                    if current and current.id != interaction.user.id:
                        await _reply(interaction, "الروم له مالك بالفعل. المالك الحالي فقط يقدر ينقل الملكية.")
                        return
                    old = current
                    if old and old.id != member.id:
                        old_ow = channel.overwrites_for(old)
                        old_ow.manage_channels = False
                        old_ow.move_members = False
                        await channel.set_permissions(old, overwrite=old_ow)
                    overwrite.view_channel = True
                    overwrite.connect = True
                    overwrite.manage_channels = True
                    overwrite.move_members = True
                    await channel.set_permissions(member, overwrite=overwrite, reason="نقل ملكية الروم")
                    msg = f"تم نقل ملكية الروم إلى {member.mention}."
            else:
                msg = "تم."
            await _reply(interaction, msg)
        except ValueError:
            await _reply(interaction, "القيمة غير صحيحة. تأكد من الرقم المطلوب.")
        except discord.Forbidden:
            await _reply(interaction, "البوت ما عنده صلاحية كافية لتنفيذ العملية.")
        except discord.HTTPException:
            logger.exception("Temporary voice panel action failed")
            await _reply(interaction, "تعذر تنفيذ العملية. جرّب مرة ثانية.")


class KnockModal(discord.ui.Modal, title="طلب دخول الروم"):
    room_id = discord.ui.TextInput(label="آيدي الروم الصوتي", max_length=25)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            channel = interaction.guild.get_channel(int(self.room_id.value)) if interaction.guild else None
            if not isinstance(channel, discord.VoiceChannel) or channel.id not in _all_temp_ids(interaction.client):
                await _reply(interaction, "آيدي الروم غير صحيح أو ليس رومًا مؤقتًا.")
                return
            owner = _owner(channel)
            if owner:
                await owner.send(f"العضو {interaction.user.mention} يطلب الدخول إلى رومك {channel.mention}.")
                await _reply(interaction, "أرسلنا طلبك لمالك الروم.")
            else:
                await _reply(interaction, "ما قدرنا نحدد مالك الروم.")
        except discord.Forbidden:
            await _reply(interaction, "تعذر إرسال الطلب؛ قد تكون الرسائل الخاصة مقفلة.")
        except (ValueError, discord.HTTPException):
            await _reply(interaction, "تعذر إرسال الطلب.")


class TempVoicePanel(discord.ui.View):
    """Small, reliable panel with only the requested supported controls."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="تغيير الاسم", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="tempvoice:rename", row=0)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await _room_or_reply(interaction)
        if channel is not None:
            await interaction.response.send_modal(
                ArabicRoomModal(
                    "تغيير اسم الروم",
                    "الاسم الجديد",
                    "room_name",
                    default=channel.name.removeprefix("🔊・"),
                )
            )

    @discord.ui.button(label="قفل الروم", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="tempvoice:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await _room_or_reply(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = False
        try:
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=overwrite,
                reason=f"قفل الروم بواسطة {interaction.user}",
            )
            await _reply(interaction, "تم قفل الروم.")
        except discord.Forbidden:
            await _reply(interaction, "البوت ما عنده صلاحية كافية لقفل الروم.")
        except discord.HTTPException:
            logger.exception("Could not lock temporary room %s", channel.id)
            await _reply(interaction, "تعذر قفل الروم. حاول مرة ثانية.")

    @discord.ui.button(label="فتح الروم", emoji="🔓", style=discord.ButtonStyle.secondary, custom_id="tempvoice:unlock", row=0)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await _room_or_reply(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = None
        try:
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=overwrite,
                reason=f"فتح الروم بواسطة {interaction.user}",
            )
            await _reply(interaction, "تم فتح الروم.")
        except discord.Forbidden:
            await _reply(interaction, "البوت ما عنده صلاحية كافية لفتح الروم.")
        except discord.HTTPException:
            logger.exception("Could not unlock temporary room %s", channel.id)
            await _reply(interaction, "تعذر فتح الروم. حاول مرة ثانية.")

    @discord.ui.button(label="حد الأعضاء", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="tempvoice:limit", row=0)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await _room_or_reply(interaction)
        if channel is not None:
            await interaction.response.send_modal(
                ArabicRoomModal(
                    "حد الأعضاء",
                    "عدد الأعضاء من 0 إلى 99",
                    "room_limit",
                    default=str(channel.user_limit),
                    max_length=2,
                )
            )

    @discord.ui.button(label="حذف الروم", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="tempvoice:delete", row=1)
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await _room_or_reply(interaction)
        if channel is None:
            return
        await interaction.response.send_message("جارٍ حذف رومك المؤقت.", ephemeral=True)
        cog = interaction.client.get_cog("TemporaryVoice")
        if cog:
            cog.temporary_channel_ids.discard(channel.id)
        try:
            await channel.delete(reason=f"حذف الروم بواسطة {interaction.user}")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning("Missing permission to delete temporary room %s", channel.id)
            if cog:
                cog.temporary_channel_ids.add(channel.id)
            await interaction.followup.send("البوت ما عنده صلاحية لحذف الروم.", ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not delete temporary room %s", channel.id)
            if cog:
                cog.temporary_channel_ids.add(channel.id)
            await interaction.followup.send("تعذر حذف الروم. حاول مرة ثانية.", ephemeral=True)


class RegionSelect(discord.ui.View):
    def __init__(self, channel_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=60)
        self.channel_id = channel_id
        select = discord.ui.Select(placeholder="اختر المنطقة", options=options, custom_id="tempvoice:region_select")
        select.callback = self.select_region
        self.add_item(select)

    async def select_region(self, interaction: discord.Interaction) -> None:
        channel = _get_owned_temp_channel(interaction)
        if channel is None or channel.id != self.channel_id:
            await _reply(interaction, "ادخل رومك المؤقت الذي تملكه.")
            return
        try:
            await channel.edit(rtc_region=None if interaction.data["values"][0] == "auto" else interaction.data["values"][0])
            await _reply(interaction, "تم تحديث منطقة الصوت.")
        except discord.HTTPException:
            await _reply(interaction, "تعذر تغيير المنطقة؛ قد لا تكون المنطقة مدعومة.")


async def setup(bot: commands.Bot) -> None:
    bot.add_view(TempVoicePanel())
    await bot.add_cog(TemporaryVoice(bot))
