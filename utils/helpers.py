"""
utils/helpers.py
Shared utilities used across all cogs.
"""

import discord
import logging
import asyncio
from typing import Optional, Union

log = logging.getLogger("bot.helpers")


# ── Permission check ──────────────────────────────────────────────────────────
def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_channels

def is_mod(member: discord.Member) -> bool:
    return member.guild_permissions.manage_channels

def require_admin(interaction: discord.Interaction) -> bool:
    return is_admin(interaction.user)

def require_mod(interaction: discord.Interaction) -> bool:
    return is_mod(interaction.user)


# ── Audit log poster ──────────────────────────────────────────────────────────
async def post_audit(
    bot,
    guild_id: int,
    *,
    executor: discord.Member,
    target: Union[discord.Member, discord.Role, str],
    action: str,
    field: str = None,
    before=None,
    after=None,
    note: str = None,
    txn_id: str = None,
    flagged: bool = False,
):
    channel_id = await bot.db.get_config(guild_id, "audit_log_channel")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return

    colour = discord.Colour.red() if flagged else discord.Colour.blurple()
    embed = discord.Embed(
        title=f"{'🚨 FLAGGED — ' if flagged else ''}Audit Log: {action}",
        colour=colour,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Executor", value=executor.mention, inline=True)
    if isinstance(target, discord.Member):
        tval = target.mention
    elif isinstance(target, discord.Role):
        tval = target.mention
    else:
        tval = str(target)
    embed.add_field(name="Target", value=tval, inline=True)
    if field:
        embed.add_field(name="Field", value=field, inline=True)
    if before is not None:
        embed.add_field(name="Before", value=str(before), inline=True)
    if after is not None:
        embed.add_field(name="After", value=str(after), inline=True)
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    if txn_id:
        embed.set_footer(text=f"TXN: {txn_id}")
    try:
        await channel.send(embed=embed)
    except Exception as e:
        log.error(f"Failed to post audit log: {e}")


# ── Critical alert poster ─────────────────────────────────────────────────────
async def post_alert(
    bot, guild_id: Optional[int], message: str, error: Exception = None
):
    """Post to a guild's alert channel. Pass guild_id=None to broadcast to all guilds."""
    guild_ids = [guild_id] if guild_id is not None else [g.id for g in bot.guilds]
    embed = discord.Embed(
        title="🚨 Critical Alert",
        description=message,
        colour=discord.Colour.red(),
        timestamp=discord.utils.utcnow(),
    )
    if error:
        embed.add_field(name="Error", value=f"```{str(error)[:500]}```")
    for gid in guild_ids:
        channel_id = await bot.db.get_config(gid, "alert_channel")
        if not channel_id:
            log.critical(f"ALERT (guild={gid}, no channel): {message}")
            continue
        channel = bot.get_channel(int(channel_id))
        if not channel:
            continue
        try:
            await channel.send(embed=embed)
        except Exception as e:
            log.error(f"Failed to post alert to guild={gid}: {e}")


# ── User DM notification ──────────────────────────────────────────────────────
async def notify_user(
    bot,
    user: discord.User,
    *,
    title: str,
    description: str,
    colour: discord.Colour = discord.Colour.green(),
):
    """DM a user about a balance/item change. Silently fails if DMs are closed."""
    try:
        embed = discord.Embed(title=title, description=description, colour=colour)
        embed.set_footer(text="This is an automated notification from the economy system.")
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ── Confirmation view ─────────────────────────────────────────────────────────
class ConfirmView(discord.ui.View):
    def __init__(self, executor_id: int, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.executor_id = executor_id
        self.value: Optional[bool] = None
        self._lock = asyncio.Lock()
        self._responded = False

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.executor_id:
            await interaction.response.send_message(
                "Only the command executor can respond.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Yes, continue", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        async with self._lock:
            if self._responded:
                await interaction.response.send_message("Already responded.", ephemeral=True)
                return
            self._responded = True
            self.value = True
        self._disable_all()
        await interaction.response.edit_message(content="✅ Confirmed. Executing…", view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        async with self._lock:
            if self._responded:
                await interaction.response.send_message("Already responded.", ephemeral=True)
                return
            self._responded = True
            self.value = False
        self._disable_all()
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)
        self.stop()

    async def on_timeout(self):
        self._disable_all()
        self.value = None

    def _disable_all(self):
        for item in self.children:
            item.disabled = True


# ── Execution lock ─────────────────────────────────────────────────────────────
_exec_locks: dict[int, asyncio.Lock] = {}

def get_exec_lock(executor_id: int) -> asyncio.Lock:
    if executor_id not in _exec_locks:
        _exec_locks[executor_id] = asyncio.Lock()
    return _exec_locks[executor_id]


# ── Embeds ─────────────────────────────────────────────────────────────────────
def success_embed(title: str, description: str = None) -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=description,
                         colour=discord.Colour.green())

def error_embed(title: str, description: str = None) -> discord.Embed:
    return discord.Embed(title=f"❌ {title}", description=description,
                         colour=discord.Colour.red())

def info_embed(title: str, description: str = None) -> discord.Embed:
    return discord.Embed(title=f"ℹ️ {title}", description=description,
                         colour=discord.Colour.blurple())

def warn_embed(title: str, description: str = None) -> discord.Embed:
    return discord.Embed(title=f"⚠️ {title}", description=description,
                         colour=discord.Colour.yellow())


# ── Format currency ────────────────────────────────────────────────────────────
def fmt_money(amount: int, symbol: str = "💰") -> str:
    return f"{symbol} {amount:,}"
