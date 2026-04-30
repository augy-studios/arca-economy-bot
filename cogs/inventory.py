"""
cogs/inventory.py
Slash commands: /inventory edit | view | trade | give
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import json
import logging

from utils.helpers import (
    require_mod, is_mod,
    post_audit, notify_user,
    ConfirmView, get_exec_lock,
    success_embed, error_embed, info_embed, warn_embed
)

log = logging.getLogger("bot.inventory")


class Inventory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    inv = app_commands.Group(name="inventory", description="Inventory management commands")

    # ── /inventory view ───────────────────────────────────────────────────────
    @inv.command(name="view", description="View your (or another user's) inventory.")
    @app_commands.describe(user="User to inspect (leave blank for yourself)")
    async def inv_view(self, interaction: discord.Interaction,
                       user: Optional[discord.Member] = None):
        target = user or interaction.user
        rows = await self.bot.db.get_user_inventory(interaction.guild_id, target.id)

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed(f"{target.display_name}'s Inventory", "Empty inventory."),
                ephemeral=True
            )

        embed = info_embed(f"🎒 {target.display_name}'s Inventory")
        embed.set_thumbnail(url=target.display_avatar.url)
        for row in rows:
            embed.add_field(
                name=f"{row['item_name']} ×{row['quantity']}",
                value=row['item_desc'] or "No description.",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /inventory edit ───────────────────────────────────────────────────────
    @inv.command(name="edit", description="[MOD] Add or remove an item from a user or role.")
    @app_commands.describe(
        action="Add or Remove",
        target="User or Role mention/ID",
        item_name="Name of the item",
        quantity="How many (default 1)",
        note="Optional reason"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add",    value="add"),
        app_commands.Choice(name="Remove", value="remove"),
    ])
    async def inv_edit(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        target: str,
        item_name: str,
        quantity: Optional[int] = 1,
        note: Optional[str] = None,
    ):
        if not require_mod(interaction):
            return await interaction.response.send_message(
                embed=error_embed("No Permission"), ephemeral=True
            )
        if quantity is None or quantity <= 0:
            return await interaction.response.send_message(
                embed=error_embed("Invalid Quantity"), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id
        guild = interaction.guild
        gcfg = await self.bot.db.get_guild_settings(gid)
        mention_id = target.strip("<@&!> ")
        try:
            mid = int(mention_id)
        except ValueError:
            return await interaction.followup.send(
                embed=error_embed("Invalid Target"), ephemeral=True
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
                embed=error_embed("Not Found"), ephemeral=True
            )

        item = await self.bot.db.get_shop_item_by_name(gid, item_name)
        item_id = item["item_id"] if item else item_name.lower().replace(" ", "_")
        delta = quantity if action.value == "add" else -quantity

        if len(targets) > 1:
            view = ConfirmView(interaction.user.id, timeout=gcfg.confirm_timeout_seconds)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Bulk Inventory Edit",
                    description=(
                        f"This will **{action.value}** ×{quantity} **{item_name}** "
                        f"for **{len(targets)} users** in {role.mention}.\nContinue?"
                    ),
                    colour=discord.Colour.orange()
                ),
                view=view, ephemeral=True
            )
            await view.wait()
            if not view.value:
                return

        async with get_exec_lock(interaction.user.id):
            succeeded, failed = 0, 0
            for m in targets:
                await self.bot.db.ensure_user(gid, m.id)
                result = await self.bot.db.modify_inventory(
                    guild_id=gid,
                    executor_id=interaction.user.id,
                    user_id=m.id,
                    item_id=item_id,
                    delta=delta,
                    note=note or f"{action.value} by mod",
                )
                if result["ok"]:
                    succeeded += 1
                    if action.value == "add":
                        await notify_user(
                            self.bot, m,
                            title="🎁 Item Received!",
                            description=(
                                f"You received ×{quantity} **{item_name}**"
                                + (f"\n**Reason:** {note}" if note else "")
                            )
                        )
                    else:
                        await notify_user(
                            self.bot, m,
                            title="📦 Item Removed",
                            description=f"×{quantity} **{item_name}** was removed from your inventory."
                            + (f"\n**Reason:** {note}" if note else ""),
                            colour=discord.Colour.orange()
                        )
                else:
                    failed += 1

        await post_audit(
            self.bot, gid,
            executor=interaction.user,
            target=target_obj,
            action=f"inventory_{action.value}",
            field=item_name,
            before=None,
            after=f"×{quantity} × {succeeded} users",
            note=note,
        )
        summary = f"✅ {succeeded} succeeded"
        if failed:
            summary += f", ❌ {failed} failed"
        await interaction.edit_original_response(
            embed=success_embed("Inventory Edit Complete", summary), view=None
        )

    # ── /inventory give ───────────────────────────────────────────────────────
    @inv.command(name="give", description="Give an item from your inventory to another user.")
    @app_commands.describe(
        user="Recipient",
        item_name="Item name",
        quantity="How many to give (default 1)"
    )
    async def inv_give(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        item_name: str,
        quantity: Optional[int] = 1,
    ):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id
        gcfg = await self.bot.db.get_guild_settings(gid)

        if user.id == interaction.user.id:
            return await interaction.followup.send(
                embed=error_embed("Self-Gift Blocked"), ephemeral=True
            )
        if user.bot:
            return await interaction.followup.send(
                embed=error_embed("Invalid Target"), ephemeral=True
            )

        if await self.bot.db.check_rate_limit(gid, interaction.user.id, "inv_give", gcfg.rate_limit_seconds):
            return await interaction.followup.send(
                embed=error_embed("Slow down!"), ephemeral=True
            )

        item = await self.bot.db.get_shop_item_by_name(gid, item_name)
        item_id = item["item_id"] if item else item_name.lower().replace(" ", "_")

        if await self.bot.db.is_blacklisted_alt(gid, user.id):
            return await interaction.followup.send(
                embed=error_embed("Blocked", "That account cannot receive items."), ephemeral=True
            )

        if await self.bot.db.check_gift_cooldown(gid, interaction.user.id, user.id):
            return await interaction.followup.send(
                embed=error_embed(
                    "Cooldown Active",
                    f"You can only gift to the same user once every {gcfg.gift_cooldown_hours}h."
                ),
                ephemeral=True
            )

        result = await self.bot.db.gift_item(gid, interaction.user.id, user.id, item_id, quantity)
        await self.bot.db.update_rate_limit(gid, interaction.user.id, "inv_give")

        if not result["ok"]:
            return await interaction.followup.send(
                embed=error_embed("Transfer Failed", result["error"]), ephemeral=True
            )

        flooded = await self.bot.db.check_gift_flood(gid, interaction.user.id)
        if flooded:
            from utils.helpers import post_alert
            await post_alert(
                self.bot, gid,
                f"🚨 Item gift flood: <@{interaction.user.id}> is rapidly gifting items."
            )

        await post_audit(
            self.bot, gid,
            executor=interaction.user,
            target=user,
            action="inventory_give",
            field=result.get("item_name", item_name),
            before=None,
            after=f"×{quantity}",
            flagged=flooded,
        )
        await notify_user(
            self.bot, user,
            title="🎁 Item Received!",
            description=(
                f"{interaction.user.mention} gave you ×{quantity} "
                f"**{result.get('item_name', item_name)}**!"
            )
        )
        await interaction.followup.send(
            embed=success_embed(
                "Item Transferred",
                f"Gave ×{quantity} **{result.get('item_name', item_name)}** to {user.mention}."
            ),
            ephemeral=True
        )

    # ── /trade ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="trade", description="Propose a trade with another user.")
    @app_commands.describe(
        user="User to trade with",
        offer_cash="Cash you are offering",
        offer_item="Item name you are offering (optional)",
        offer_qty="Quantity of item to offer",
        request_cash="Cash you want in return",
        request_item="Item name you want in return (optional)",
        request_qty="Quantity of item you want",
    )
    async def trade(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        offer_cash: Optional[int] = 0,
        offer_item: Optional[str] = None,
        offer_qty: Optional[int] = 1,
        request_cash: Optional[int] = 0,
        request_item: Optional[str] = None,
        request_qty: Optional[int] = 1,
    ):
        await interaction.response.defer(ephemeral=False)
        gid = interaction.guild_id
        gcfg = await self.bot.db.get_guild_settings(gid)

        if user.id == interaction.user.id or user.bot:
            return await interaction.followup.send(
                embed=error_embed("Invalid Target"), ephemeral=True
            )
        if offer_cash < 0 or request_cash < 0:
            return await interaction.followup.send(
                embed=error_embed("Invalid Amount"), ephemeral=True
            )

        offer_items_json = json.dumps({"item": offer_item, "qty": offer_qty}) if offer_item else None
        request_items_json = json.dumps({"item": request_item, "qty": request_qty}) if request_item else None

        if offer_cash > 0:
            row = await self.bot.db.get_user(gid, interaction.user.id)
            if row["cash"] < offer_cash:
                return await interaction.followup.send(
                    embed=error_embed("Insufficient Cash"), ephemeral=True
                )
        if offer_item:
            item = await self.bot.db.get_shop_item_by_name(gid, offer_item)
            item_id = item["item_id"] if item else offer_item.lower().replace(" ", "_")
            inv = await self.bot.db.get_inventory_item(gid, interaction.user.id, item_id)
            if not inv or inv["quantity"] < offer_qty:
                return await interaction.followup.send(
                    embed=error_embed("Insufficient Items", f"You don't have ×{offer_qty} {offer_item}."),
                    ephemeral=True
                )

        result = await self.bot.db.create_trade(
            gid, interaction.user.id, user.id,
            offer_cash, offer_items_json,
            request_cash, request_items_json
        )
        if not result["ok"]:
            return await interaction.followup.send(
                embed=error_embed("Trade Failed", result["error"]), ephemeral=True
            )

        trade_id = result["trade_id"]
        offer_parts = []
        if offer_cash:
            offer_parts.append(gcfg.fmt_money(offer_cash))
        if offer_item:
            offer_parts.append(f"×{offer_qty} {offer_item}")
        req_parts = []
        if request_cash:
            req_parts.append(gcfg.fmt_money(request_cash))
        if request_item:
            req_parts.append(f"×{request_qty} {request_item}")

        embed = discord.Embed(
            title="🔄 Trade Offer",
            description=f"{interaction.user.mention} wants to trade with {user.mention}",
            colour=discord.Colour.blurple()
        )
        embed.add_field(name="They Offer", value=", ".join(offer_parts) or "Nothing", inline=True)
        embed.add_field(name="They Want",  value=", ".join(req_parts)   or "Nothing", inline=True)
        embed.set_footer(text=f"Trade ID: {trade_id[:8]}… | Expires in {gcfg.trade_timeout_seconds}s")

        view = TradeView(self.bot, trade_id, interaction.user, user, gid,
                         timeout=gcfg.trade_timeout_seconds)
        msg = await interaction.followup.send(content=user.mention, embed=embed, view=view)
        view.message = msg


class TradeView(discord.ui.View):
    def __init__(
        self, bot, trade_id: str,
        initiator: discord.Member, target: discord.Member,
        guild_id: int, timeout: int = 120,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.trade_id = trade_id
        self.initiator = initiator
        self.target = target
        self.guild_id = guild_id
        self.message = None

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Only the trade target can accept.", ephemeral=True
            )

        trade = await self.bot.db.get_trade(self.trade_id)
        if not trade or trade["status"] != "pending":
            return await interaction.response.send_message(
                "Trade is no longer valid.", ephemeral=True
            )

        ok = True
        gid = self.guild_id
        note = f"Trade {self.trade_id[:8]}"

        if trade["offer_cash"] > 0:
            r = await self.bot.db.modify_balance(
                gid, self.bot.application_id, trade["initiator_id"],
                "cash", -trade["offer_cash"], note=note
            )
            if not r["ok"]:
                ok = False
            else:
                await self.bot.db.modify_balance(
                    gid, self.bot.application_id, trade["target_id"],
                    "cash", trade["offer_cash"], note=note
                )
        if trade["request_cash"] > 0 and ok:
            r = await self.bot.db.modify_balance(
                gid, self.bot.application_id, trade["target_id"],
                "cash", -trade["request_cash"], note=note
            )
            if not r["ok"]:
                ok = False
            else:
                await self.bot.db.modify_balance(
                    gid, self.bot.application_id, trade["initiator_id"],
                    "cash", trade["request_cash"], note=note
                )

        if not ok:
            await self.bot.db.resolve_trade(self.trade_id, "cancelled")
            for item in self.children:
                item.disabled = True
            return await interaction.response.edit_message(
                embed=error_embed("Trade Failed", "One party had insufficient funds/items."),
                view=self
            )

        await self.bot.db.resolve_trade(self.trade_id, "completed")
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=success_embed("Trade Complete", "The trade was successfully completed!"),
            view=self
        )

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id not in (self.target.id, self.initiator.id):
            return await interaction.response.send_message(
                "You are not part of this trade.", ephemeral=True
            )
        await self.bot.db.resolve_trade(self.trade_id, "cancelled")
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=error_embed("Trade Declined"), view=self
        )

    async def on_timeout(self):
        await self.bot.db.resolve_trade(self.trade_id, "expired")
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    embed=warn_embed("Trade Expired", "The trade offer has timed out."),
                    view=self
                )
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(Inventory(bot))
