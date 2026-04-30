"""
cogs/admin.py
Slash commands: /config | /leaderboard | /auditlog
"""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
from typing import Optional
import json
import logging
from datetime import datetime, timezone

from utils.helpers import (
    require_admin, require_mod,
    post_audit, post_alert,
    success_embed, error_embed, info_embed, warn_embed
)

log = logging.getLogger("bot.admin")

# key → expected value type ("channel" | "int" | "str" | "bool")
_CONFIG_TYPES: dict[str, str] = {
    "audit_log_channel":          "channel",
    "alert_channel":              "channel",
    "currency_symbol":            "str",
    "currency_name":              "str",
    "max_balance":                "int",
    "max_daily_earn":             "int",
    "gift_cooldown_hours":        "int",
    "gift_flagging_threshold":    "int",
    "gift_flagging_window_hours": "int",
    "rate_limit_seconds":         "int",
    "allow_debt":                 "bool",
    "lb_cache_ttl":               "int",
    "confirm_timeout_seconds":    "int",
    "trade_timeout_seconds":      "int",
}

_CONFIG_CHOICES = [
    app_commands.Choice(name="Audit Log Channel",            value="audit_log_channel"),
    app_commands.Choice(name="Alert Channel",                value="alert_channel"),
    app_commands.Choice(name="Currency Symbol",              value="currency_symbol"),
    app_commands.Choice(name="Currency Name",                value="currency_name"),
    app_commands.Choice(name="Max Balance",                  value="max_balance"),
    app_commands.Choice(name="Max Daily Earn",               value="max_daily_earn"),
    app_commands.Choice(name="Gift Cooldown (hours)",        value="gift_cooldown_hours"),
    app_commands.Choice(name="Gift Flag Threshold",          value="gift_flagging_threshold"),
    app_commands.Choice(name="Gift Flag Window (hours)",     value="gift_flagging_window_hours"),
    app_commands.Choice(name="Rate Limit (seconds)",         value="rate_limit_seconds"),
    app_commands.Choice(name="Allow Debt",                   value="allow_debt"),
    app_commands.Choice(name="Leaderboard Cache TTL (s)",    value="lb_cache_ttl"),
    app_commands.Choice(name="Confirm Timeout (seconds)",    value="confirm_timeout_seconds"),
    app_commands.Choice(name="Trade Timeout (seconds)",      value="trade_timeout_seconds"),
]


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.leaderboard_refresh.start()
        self.backup_loop.start()
        self.integrity_loop.start()

    def cog_unload(self):
        self.leaderboard_refresh.cancel()
        self.backup_loop.cancel()
        self.integrity_loop.cancel()

    # ── Background tasks ───────────────────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def leaderboard_refresh(self):
        """Rebuild each guild's leaderboard cache when its TTL has expired."""
        try:
            now = datetime.now(timezone.utc)
            for guild in self.bot.guilds:
                gcfg = await self.bot.db.get_guild_settings(guild.id)
                cached = await self.bot.db.get_leaderboard_data(guild.id, "cash")
                if cached:
                    updated = datetime.fromisoformat(cached["updated_at"])
                    if (now - updated).total_seconds() < gcfg.lb_cache_ttl:
                        continue
                await self._rebuild_leaderboard_cache(guild.id)
        except Exception as e:
            log.error(f"Leaderboard cache refresh failed: {e}", exc_info=True)

    @tasks.loop(hours=6)
    async def backup_loop(self):
        try:
            path = await self.bot.db.backup()
            log.info(f"Scheduled backup saved: {path}")
        except Exception as e:
            log.error(f"Backup failed: {e}", exc_info=True)
            await post_alert(self.bot, None, "🚨 Scheduled database backup failed!", error=e)

    @tasks.loop(hours=6)
    async def integrity_loop(self):
        try:
            result = await self.bot.db.integrity_scan()
            if result["count"] > 0:
                log.warning(f"Integrity scan fixed {result['count']} issues: {result['fixed']}")
                await post_alert(
                    self.bot, None,
                    f"⚠️ Integrity scan fixed {result['count']} issue(s):\n"
                    + "\n".join(result["fixed"][:10])
                )
        except Exception as e:
            log.error(f"Integrity scan failed: {e}", exc_info=True)

    @leaderboard_refresh.before_loop
    @backup_loop.before_loop
    @integrity_loop.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    async def _rebuild_leaderboard_cache(self, guild_id: int):
        data = await self.bot.db.build_leaderboard(guild_id)
        for cat, rows in data.items():
            await self.bot.db.set_leaderboard_data(guild_id, cat, json.dumps(rows))
        log.info(f"Leaderboard cache rebuilt for guild {guild_id}.")

    # ── Config command group ───────────────────────────────────────────────────
    config_group = app_commands.Group(name="config", description="Bot configuration and management")

    # ── /config set ───────────────────────────────────────────────────────────
    @config_group.command(name="set", description="Set a bot configuration value.")
    @app_commands.describe(key="Config key to change", value="New value")
    @app_commands.choices(key=_CONFIG_CHOICES)
    async def config_set(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: str,
    ):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )

        kind = _CONFIG_TYPES[key.value]
        cleaned = value.strip()

        if kind == "channel":
            cleaned = cleaned.strip("<#> ")
            try:
                int(cleaned)
            except ValueError:
                return await interaction.followup.send(
                    embed=error_embed("Invalid", "Provide a numeric channel ID or #mention."),
                    ephemeral=True,
                )
        elif kind == "int":
            try:
                int(cleaned)
            except ValueError:
                return await interaction.followup.send(
                    embed=error_embed("Invalid", "Value must be a whole number."), ephemeral=True
                )
        elif kind == "bool":
            if cleaned.lower() not in ("true", "false", "1", "0", "yes", "no"):
                return await interaction.followup.send(
                    embed=error_embed("Invalid", "Use `true` or `false`."), ephemeral=True
                )
            cleaned = "true" if cleaned.lower() in ("true", "1", "yes") else "false"

        await self.bot.db.set_config(interaction.guild_id, key.value, cleaned)
        await interaction.followup.send(
            embed=success_embed("Config Updated", f"`{key.value}` → `{cleaned}`"),
            ephemeral=True,
        )

    # ── /config view ──────────────────────────────────────────────────────────
    @config_group.command(name="view", description="Show all current configuration values.")
    async def config_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        gcfg = await self.bot.db.get_guild_settings(interaction.guild_id)

        audit_ch = await self.bot.db.get_config(interaction.guild_id, "audit_log_channel")
        alert_ch = await self.bot.db.get_config(interaction.guild_id, "alert_channel")

        embed = discord.Embed(title="⚙️ Bot Configuration", colour=discord.Colour.blurple())
        embed.add_field(name="Audit Log Channel",  value=f"<#{audit_ch}>" if audit_ch else "*(not set)*", inline=True)
        embed.add_field(name="Alert Channel",      value=f"<#{alert_ch}>" if alert_ch else "*(not set)*", inline=True)
        embed.add_field(name="​", value="​", inline=True)

        embed.add_field(name="Currency Symbol",    value=gcfg.currency_symbol,           inline=True)
        embed.add_field(name="Currency Name",      value=gcfg.currency_name,             inline=True)
        embed.add_field(name="Max Balance",        value=f"{gcfg.max_balance:,}",        inline=True)
        embed.add_field(name="Max Daily Earn",     value=f"{gcfg.max_daily_earn:,}",     inline=True)
        embed.add_field(name="Allow Debt",         value=str(gcfg.allow_debt),           inline=True)
        embed.add_field(name="​", value="​", inline=True)

        embed.add_field(name="Gift Cooldown (h)",  value=str(gcfg.gift_cooldown_hours),        inline=True)
        embed.add_field(name="Gift Flag Threshold",value=str(gcfg.gift_flagging_threshold),     inline=True)
        embed.add_field(name="Gift Flag Window (h)",value=str(gcfg.gift_flagging_window_hours), inline=True)

        embed.add_field(name="Rate Limit (s)",     value=str(gcfg.rate_limit_seconds),         inline=True)
        embed.add_field(name="LB Cache TTL (s)",   value=str(gcfg.lb_cache_ttl),               inline=True)
        embed.add_field(name="Confirm Timeout (s)",value=str(gcfg.confirm_timeout_seconds),     inline=True)
        embed.add_field(name="Trade Timeout (s)",  value=str(gcfg.trade_timeout_seconds),       inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /config backup ────────────────────────────────────────────────────────
    @config_group.command(name="backup", description="Manually trigger a database backup.")
    async def admin_backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        try:
            path = await self.bot.db.backup()
            await interaction.followup.send(
                embed=success_embed("Backup Complete", f"Saved to `{path}`"), ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                embed=error_embed("Backup Failed", str(e)), ephemeral=True
            )

    # ── /config integrity ─────────────────────────────────────────────────────
    @config_group.command(name="integrity", description="Run database integrity scan now.")
    async def admin_integrity(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        try:
            result = await self.bot.db.integrity_scan()
        except Exception as e:
            log.error(f"integrity_scan command error: {e}", exc_info=True)
            return await interaction.followup.send(
                embed=error_embed("Integrity Scan Failed", str(e)), ephemeral=True
            )
        if result["count"] == 0:
            await interaction.followup.send(
                embed=success_embed("Integrity OK", "No issues found."), ephemeral=True
            )
        else:
            desc = "\n".join(result["fixed"][:20])
            await interaction.followup.send(
                embed=warn_embed(f"Fixed {result['count']} Issue(s)", desc), ephemeral=True
            )

    # ── /config blacklist ─────────────────────────────────────────────────────
    @config_group.command(name="blacklist_add", description="Blacklist an account from receiving gifts.")
    @app_commands.describe(user="User to blacklist", reason="Reason")
    async def blacklist_add(self, interaction: discord.Interaction,
                            user: discord.Member, reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        result = await self.bot.db.add_blacklisted_alt(
            interaction.guild_id, user.id, interaction.user.id, reason
        )
        if not result["ok"]:
            return await interaction.followup.send(
                embed=error_embed("Failed", result.get("error")), ephemeral=True
            )
        await post_audit(
            self.bot, interaction.guild_id,
            executor=interaction.user, target=user,
            action="blacklist_add", note=reason
        )
        await interaction.followup.send(
            embed=success_embed("Blacklisted", f"{user.mention} added to alt blacklist."),
            ephemeral=True
        )

    @config_group.command(name="blacklist_remove", description="Remove a user from the alt blacklist.")
    @app_commands.describe(user="User to unblacklist")
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        await self.bot.db.remove_blacklisted_alt(interaction.guild_id, user.id)
        await interaction.followup.send(
            embed=success_embed("Removed", f"{user.mention} removed from blacklist."), ephemeral=True
        )

    # ── /leaderboard ──────────────────────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="View the server economy leaderboard.")
    @app_commands.describe(category="What to rank by")
    @app_commands.choices(category=[
        app_commands.Choice(name="💵 Cash",       value="cash"),
        app_commands.Choice(name="🏦 Bank",       value="bank"),
        app_commands.Choice(name="💰 Total",      value="total"),
        app_commands.Choice(name="💸 Most Spent", value="total_spent"),
        app_commands.Choice(name="🎒 Most Items", value="inv_count"),
    ])
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] = None,
    ):
        await interaction.response.defer()
        cat = category.value if category else "total"

        gcfg = await self.bot.db.get_guild_settings(interaction.guild_id)
        cached = await self.bot.db.get_leaderboard_data(interaction.guild_id, cat)
        if not cached:
            await self._rebuild_leaderboard_cache(interaction.guild_id)
            cached = await self.bot.db.get_leaderboard_data(interaction.guild_id, cat)

        if not cached:
            return await interaction.followup.send(
                embed=info_embed("Leaderboard", "No data yet."), ephemeral=True
            )

        data = json.loads(cached["data"])
        updated = cached["updated_at"]

        label_map = {
            "cash":        "💵 Cash",
            "bank":        "🏦 Bank",
            "total":       "💰 Total Wealth",
            "total_spent": "💸 Most Spent",
            "inv_count":   "🎒 Most Items",
        }

        embed = discord.Embed(
            title=f"🏆 Leaderboard — {label_map.get(cat, cat)}",
            colour=discord.Colour.gold()
        )
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, entry in enumerate(data[:20], start=1):
            medal = medals[i - 1] if i <= 3 else f"`{i}.`"
            uid = entry["user_id"]
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            val = entry["value"]
            val_str = f"×{val:,} items" if cat == "inv_count" else gcfg.fmt_money(val)
            lines.append(f"{medal} **{name}** — {val_str}")

        embed.description = "\n".join(lines) if lines else "No data."
        embed.set_footer(text=f"Last updated: {updated[:16]} UTC")
        await interaction.followup.send(embed=embed)

    # ── /auditlog ─────────────────────────────────────────────────────────────
    @app_commands.command(name="auditlog", description="[MOD] View recent audit log entries.")
    @app_commands.describe(
        user="Filter by target user (optional)",
        page="Page number (default 1)"
    )
    async def auditlog(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        page: Optional[int] = 1,
    ):
        await interaction.response.defer(ephemeral=True)
        if not require_mod(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        if page < 1:
            page = 1
        per_page = 10
        offset = (page - 1) * per_page
        logs = await self.bot.db.get_audit_logs(
            interaction.guild_id,
            limit=per_page, offset=offset,
            user_id=user.id if user else None
        )
        if not logs:
            return await interaction.followup.send(
                embed=info_embed("Audit Log", "No entries found."), ephemeral=True
            )

        embed = discord.Embed(
            title=f"📋 Audit Log{' — ' + user.display_name if user else ''}  (Page {page})",
            colour=discord.Colour.blurple()
        )
        for entry in logs:
            ts = entry["created_at"][:16]
            before = entry["before_value"] or "—"
            after = entry["after_value"] or "—"
            note = entry["note"] or ""
            txn = entry["transaction_id"]
            embed.add_field(
                name=f"{ts} | {entry['action']}",
                value=(
                    f"Executor: <@{entry['executor_id']}> → Target: `{entry['target_id']}`\n"
                    f"Field: `{entry['field'] or '—'}` | {before} → {after}"
                    + (f"\nNote: {note}" if note else "")
                    + (f"\nTXN: `{txn[:8]}…`" if txn else "")
                ),
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /config refreshlb ─────────────────────────────────────────────────────
    @config_group.command(name="refreshlb", description="Force a leaderboard cache refresh.")
    async def refresh_lb(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not require_admin(interaction):
            return await interaction.followup.send(
                embed=error_embed("No Permission"), ephemeral=True
            )
        await self._rebuild_leaderboard_cache(interaction.guild_id)
        await interaction.followup.send(
            embed=success_embed("Leaderboard Refreshed"), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Admin(bot))
