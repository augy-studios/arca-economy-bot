"""
cogs/banking.py
Slash commands: /money add | remove | give | reset | balance
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import logging

from utils.helpers import (
    require_admin, require_mod, is_mod,
    post_audit, notify_user,
    ConfirmView, get_exec_lock,
    success_embed, error_embed, info_embed
)

log = logging.getLogger("bot.banking")

FIELD_CHOICES = [
    app_commands.Choice(name="Cash", value="cash"),
    app_commands.Choice(name="Bank", value="bank"),
]


class Banking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    money = app_commands.Group(name="money", description="Economy banking commands")

    # ── /money balance ────────────────────────────────────────────────────────
    @money.command(name="balance", description="Check your (or another user's) balance.")
    @app_commands.describe(user="User to check (leave blank for yourself)")
    async def balance(self, interaction: discord.Interaction,
                      user: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user
        gcfg = await self.bot.db.get_guild_settings(interaction.guild_id)
        row = await self.bot.db.get_user(interaction.guild_id, target.id)
        total = row["cash"] + row["bank"]
        embed = info_embed(
            f"{target.display_name}'s Balance",
            f"💵 **Cash:** {gcfg.fmt_money(row['cash'])}\n"
            f"🏦 **Bank:** {gcfg.fmt_money(row['bank'])}\n"
            f"💰 **Total:** {gcfg.fmt_money(total)}"
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /money add ────────────────────────────────────────────────────────────
    @money.command(name="add", description="[MOD] Add money to a user or role.")
    @app_commands.describe(
        target="User or Role to credit",
        amount="Amount to add",
        field="Cash or Bank wallet",
        note="Optional reason"
    )
    @app_commands.choices(field=FIELD_CHOICES)
    async def money_add(
        self,
        interaction: discord.Interaction,
        target: str,
        amount: int,
        field: app_commands.Choice[str] = None,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not require_mod(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission", "You need Manage Channels to use this."),
                ephemeral=True
            )
        if amount <= 0:
            return await interaction.followup.send(
                embed=error_embed("Invalid Amount", "Amount must be positive."), ephemeral=True
            )
        wallet = field.value if field else "cash"
        await self._bulk_money_op(interaction, target, amount, wallet, "add", note or "")

    # ── /money remove ─────────────────────────────────────────────────────────
    @money.command(name="remove", description="[MOD] Remove money from a user or role.")
    @app_commands.describe(
        target="User or Role to deduct from",
        amount="Amount to remove",
        field="Cash or Bank wallet",
        note="Optional reason"
    )
    @app_commands.choices(field=FIELD_CHOICES)
    async def money_remove(
        self,
        interaction: discord.Interaction,
        target: str,
        amount: int,
        field: app_commands.Choice[str] = None,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not require_mod(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        if amount <= 0:
            return await interaction.followup.send(
                embed=error_embed("Invalid Amount", "Amount must be positive."), ephemeral=True
            )
        wallet = field.value if field else "cash"
        await self._bulk_money_op(interaction, target, -amount, wallet, "remove", note or "")

    # ── /money give (public) ──────────────────────────────────────────────────
    @money.command(name="give", description="Give some of your cash to another user.")
    @app_commands.describe(user="User to give money to", amount="Amount to give")
    async def money_give(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
    ):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id
        gcfg = await self.bot.db.get_guild_settings(gid)

        if user.id == interaction.user.id:
            return await interaction.followup.send(
                embed=error_embed("Self-Gift Blocked", "You cannot give money to yourself."),
                ephemeral=True
            )
        if user.bot:
            return await interaction.followup.send(
                embed=error_embed("Invalid Target", "Cannot give money to bots."), ephemeral=True
            )
        if amount <= 0:
            return await interaction.followup.send(
                embed=error_embed("Invalid Amount"), ephemeral=True
            )

        if await self.bot.db.check_rate_limit(gid, interaction.user.id, "give", gcfg.rate_limit_seconds):
            return await interaction.followup.send(
                embed=error_embed("Slow down!", "You're using this command too fast."), ephemeral=True
            )

        if await self.bot.db.is_blacklisted_alt(gid, user.id):
            return await interaction.followup.send(
                embed=error_embed("Blocked", "That account is restricted from receiving gifts."),
                ephemeral=True
            )

        if await self.bot.db.check_gift_cooldown(gid, interaction.user.id, user.id):
            return await interaction.followup.send(
                embed=error_embed(
                    "Cooldown Active",
                    f"You can only gift to the same user once every {gcfg.gift_cooldown_hours}h."
                ),
                ephemeral=True
            )

        result = await self.bot.db.gift_cash(gid, interaction.user.id, user.id, amount)
        await self.bot.db.update_rate_limit(gid, interaction.user.id, "give")

        if not result["ok"]:
            return await interaction.followup.send(
                embed=error_embed("Transfer Failed", result["error"]), ephemeral=True
            )

        flooded = await self.bot.db.check_gift_flood(gid, interaction.user.id)
        if flooded:
            from utils.helpers import post_alert
            await post_alert(
                self.bot, gid,
                f"🚨 Gift flood detected! <@{interaction.user.id}> has gifted to "
                f"{gcfg.gift_flagging_threshold}+ users within "
                f"{gcfg.gift_flagging_window_hours}h. Latest: <@{user.id}>"
            )

        await post_audit(
            self.bot, gid,
            executor=interaction.user,
            target=user,
            action="money_give",
            field="cash",
            before=None,
            after=amount,
            note=f"Gift of {gcfg.fmt_money(amount)}",
            txn_id=result.get("txn_id"),
            flagged=flooded,
        )
        await notify_user(
            self.bot, user,
            title="💸 You received money!",
            description=f"{interaction.user.mention} gifted you {gcfg.fmt_money(amount)} in cash!"
        )
        await interaction.followup.send(
            embed=success_embed("Transfer Complete", f"Sent {gcfg.fmt_money(amount)} to {user.mention}."),
            ephemeral=True
        )

    # ── /money reset ──────────────────────────────────────────────────────────
    @money.command(name="reset", description="[ADMIN] Reset a user's entire balance to 0.")
    @app_commands.describe(user="User to reset")
    async def money_reset(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission", "Manage Channels required."), ephemeral=True
            )
        gid = interaction.guild_id
        gcfg = await self.bot.db.get_guild_settings(gid)

        view = ConfirmView(interaction.user.id, timeout=gcfg.confirm_timeout_seconds)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="⚠️ Confirm Balance Reset",
                description=f"This will reset **{user.mention}**'s cash and bank to **0**.\nContinue?",
                colour=discord.Colour.orange()
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return

        async with get_exec_lock(interaction.user.id):
            result = await self.bot.db.reset_balance(gid, interaction.user.id, user.id)

        if not result["ok"]:
            return await interaction.edit_original_response(
                embed=error_embed("Reset Failed", result["error"]), view=None
            )

        await post_audit(
            self.bot, gid,
            executor=interaction.user,
            target=user,
            action="money_reset",
            field="cash+bank",
            before="see log",
            after=0,
            txn_id=result.get("txn_id")
        )
        await notify_user(
            self.bot, user,
            title="💸 Balance Reset",
            description="Your cash and bank balance have been reset to 0 by a moderator.",
            colour=discord.Colour.red()
        )
        await interaction.edit_original_response(
            embed=success_embed("Balance Reset", f"{user.mention}'s balance has been reset."),
            view=None
        )

    # ── Shared bulk helper ────────────────────────────────────────────────────
    async def _bulk_money_op(
        self,
        interaction: discord.Interaction,
        raw_target: str,
        delta: int,
        field: str,
        op_name: str,
        note: str,
    ):
        gid = interaction.guild_id
        guild = interaction.guild
        gcfg = await self.bot.db.get_guild_settings(gid)
        targets: list[discord.Member] = []
        target_obj = None

        mention_id = raw_target.strip("<@&!> ")
        try:
            mid = int(mention_id)
        except ValueError:
            return await interaction.followup.send(
                embed=error_embed("Invalid Target", "Provide a @user or @role mention."),
                ephemeral=True
            )

        role = guild.get_role(mid)
        member = guild.get_member(mid)

        if role:
            targets = [m for m in role.members if not m.bot]
            target_obj = role
        elif member:
            targets = [member]
            target_obj = member
        else:
            return await interaction.followup.send(
                embed=error_embed("Not Found", "Could not find that user or role."),
                ephemeral=True
            )

        if len(targets) == 0:
            return await interaction.followup.send(
                embed=error_embed("No Targets", "No valid users found."), ephemeral=True
            )

        if len(targets) > 1:
            view = ConfirmView(interaction.user.id, timeout=gcfg.confirm_timeout_seconds)
            action_word = "credit" if delta > 0 else "deduct"
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="⚠️ Bulk Operation Confirmation",
                    description=(
                        f"This will **{action_word} {gcfg.fmt_money(abs(delta))}** "
                        f"({field}) for **{len(targets)} users** in {role.mention}.\n\nContinue?"
                    ),
                    colour=discord.Colour.orange()
                ),
                view=view,
            )
            await view.wait()
            if not view.value:
                return

        async with get_exec_lock(interaction.user.id):
            succeeded, failed = 0, 0
            for member in targets:
                await self.bot.db.ensure_user(gid, member.id)
                result = await self.bot.db.modify_balance(
                    guild_id=gid,
                    executor_id=interaction.user.id,
                    user_id=member.id,
                    field=field,
                    amount=delta,
                    note=note,
                    allow_debt=gcfg.allow_debt,
                )
                if result["ok"]:
                    succeeded += 1
                    action_desc = (
                        f"{'Added' if delta > 0 else 'Removed'} {gcfg.fmt_money(abs(delta))} "
                        f"{'to' if delta > 0 else 'from'} your {field}"
                    )
                    if note:
                        action_desc += f"\n**Reason:** {note}"
                    await notify_user(
                        self.bot, member,
                        title="💰 Balance Updated",
                        description=action_desc,
                        colour=discord.Colour.green() if delta > 0 else discord.Colour.orange()
                    )
                else:
                    failed += 1

        await post_audit(
            self.bot, gid,
            executor=interaction.user,
            target=target_obj,
            action=f"money_{op_name}",
            field=field,
            before=None,
            after=f"±{abs(delta)} × {succeeded} users",
            note=note,
        )

        summary = f"✅ {succeeded} succeeded"
        if failed:
            summary += f", ❌ {failed} failed"
        await interaction.edit_original_response(
            embed=success_embed(f"Money {op_name.capitalize()} Complete", summary),
            view=None
        )


async def setup(bot):
    await bot.add_cog(Banking(bot))
