"""
cogs/shop.py
Slash commands: /shop add | edit | remove | view | buy | iteminfo
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import logging

from utils.helpers import (
    require_mod, is_mod,
    post_audit, notify_user,
    ConfirmView, get_exec_lock,
    success_embed, error_embed, info_embed
)

log = logging.getLogger("bot.shop")


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    shop = app_commands.Group(name="shop", description="Shop management and browsing commands")

    # ── /shop view ────────────────────────────────────────────────────────────
    @shop.command(name="view", description="Browse the shop.")
    async def shop_view(self, interaction: discord.Interaction):
        gcfg = await self.bot.db.get_guild_settings(interaction.guild_id)
        items = await self.bot.db.get_shop_items(interaction.guild_id)
        if not items:
            return await interaction.response.send_message(
                embed=info_embed("Shop", "The shop is currently empty."), ephemeral=True
            )

        embed = info_embed(f"🛒 Shop  —  {gcfg.currency_name.title()}")
        for item in items:
            stock_str = "∞" if item["stock"] == -1 else str(item["stock"])
            limit_str = "∞" if item["max_per_user"] == -1 else str(item["max_per_user"])
            trade_str = "✅" if item["is_tradeable"] else "🚫"
            embed.add_field(
                name=f"{item['name']}  —  {gcfg.fmt_money(item['price'])}",
                value=(
                    f"{item['description'] or 'No description.'}\n"
                    f"Stock: **{stock_str}** | Limit/user: **{limit_str}** | Tradeable: {trade_str}"
                ),
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # ── /shop add ─────────────────────────────────────────────────────────────
    @shop.command(name="add", description="[MOD] Add a new item to the shop.")
    @app_commands.describe(
        name="Item name (must be unique)",
        price="Cost in cash",
        description="Item description",
        stock="Available stock (-1 = unlimited)",
        max_per_user="Max a user can own (-1 = unlimited)",
        role_required="Role ID required to purchase (optional)",
        reply_message="Message sent to buyer on purchase (optional)",
        tradeable="Whether users can trade/gift this item"
    )
    async def shop_add(
        self,
        interaction: discord.Interaction,
        name: str,
        price: int,
        description: Optional[str] = None,
        stock: Optional[int] = -1,
        max_per_user: Optional[int] = -1,
        role_required: Optional[str] = None,
        reply_message: Optional[str] = None,
        tradeable: Optional[bool] = True,
    ):
        if not require_mod(interaction):
            return await interaction.response.send_message(
                embed=error_embed("No Permission"), ephemeral=True
            )
        if price < 0:
            return await interaction.response.send_message(
                embed=error_embed("Invalid Price", "Price cannot be negative."), ephemeral=True
            )

        role_id = None
        if role_required:
            try:
                role_id = int(role_required.strip("<@&> "))
            except ValueError:
                return await interaction.response.send_message(
                    embed=error_embed("Invalid Role", "Provide a valid role ID or mention."),
                    ephemeral=True
                )

        existing = await self.bot.db.get_shop_item_by_name(interaction.guild_id, name)
        if existing:
            return await interaction.response.send_message(
                embed=error_embed("Duplicate", f"An item named **{name}** already exists."),
                ephemeral=True
            )

        gcfg = await self.bot.db.get_guild_settings(interaction.guild_id)
        result = await self.bot.db.add_shop_item(
            guild_id=interaction.guild_id,
            executor_id=interaction.user.id,
            name=name,
            description=description or "",
            price=price,
            stock=stock if stock is not None else -1,
            max_per_user=max_per_user if max_per_user is not None else -1,
            role_required=role_id,
            reply_message=reply_message or "",
            is_tradeable=tradeable if tradeable is not None else True,
        )
        if not result["ok"]:
            return await interaction.response.send_message(
                embed=error_embed("Failed", result["error"]), ephemeral=True
            )

        await post_audit(
            self.bot, interaction.guild_id,
            executor=interaction.user,
            target="Shop",
            action="shop_item_add",
            field=name,
            after=f"price={price} stock={stock}",
        )
        stock_str = "∞" if stock == -1 else str(stock)
        await interaction.response.send_message(
            embed=success_embed(
                "Item Added",
                f"**{name}** added to shop.\nPrice: {gcfg.fmt_money(price)} | Stock: {stock_str}"
            ),
            ephemeral=True
        )

    # ── /shop edit ────────────────────────────────────────────────────────────
    @shop.command(name="edit", description="[MOD] Edit an existing shop item.")
    @app_commands.describe(
        item_name="Name of the item to edit",
        new_name="New name (optional)",
        price="New price (optional)",
        description="New description (optional)",
        stock="New stock level (optional)",
        max_per_user="New per-user limit (optional)",
        reply_message="New reply message (optional)",
        tradeable="Change tradeability (optional)"
    )
    async def shop_edit(
        self,
        interaction: discord.Interaction,
        item_name: str,
        new_name: Optional[str] = None,
        price: Optional[int] = None,
        description: Optional[str] = None,
        stock: Optional[int] = None,
        max_per_user: Optional[int] = None,
        reply_message: Optional[str] = None,
        tradeable: Optional[bool] = None,
    ):
        if not require_mod(interaction):
            return await interaction.response.send_message(
                embed=error_embed("No Permission"), ephemeral=True
            )

        item = await self.bot.db.get_shop_item_by_name(interaction.guild_id, item_name)
        if not item:
            return await interaction.response.send_message(
                embed=error_embed("Not Found", f"No item called **{item_name}**."), ephemeral=True
            )

        updates = {}
        if new_name is not None:
            updates["name"] = new_name
        if price is not None:
            updates["price"] = price
        if description is not None:
            updates["description"] = description
        if stock is not None:
            updates["stock"] = stock
        if max_per_user is not None:
            updates["max_per_user"] = max_per_user
        if reply_message is not None:
            updates["reply_message"] = reply_message
        if tradeable is not None:
            updates["is_tradeable"] = int(tradeable)

        result = await self.bot.db.edit_shop_item(
            interaction.guild_id, interaction.user.id, item["item_id"], **updates
        )
        if not result["ok"]:
            return await interaction.response.send_message(
                embed=error_embed("Edit Failed", result["error"]), ephemeral=True
            )

        await post_audit(
            self.bot, interaction.guild_id,
            executor=interaction.user,
            target="Shop",
            action="shop_item_edit",
            field=item_name,
            after=str(updates),
        )
        await interaction.response.send_message(
            embed=success_embed("Item Updated", f"**{item_name}** has been updated."),
            ephemeral=True
        )

    # ── /shop remove ──────────────────────────────────────────────────────────
    @shop.command(name="remove", description="[MOD] Remove an item from the shop (soft delete).")
    @app_commands.describe(item_name="Name of the item to remove")
    async def shop_remove(self, interaction: discord.Interaction, item_name: str):
        if not require_mod(interaction):
            return await interaction.response.send_message(
                embed=error_embed("No Permission"), ephemeral=True
            )

        item = await self.bot.db.get_shop_item_by_name(interaction.guild_id, item_name)
        if not item:
            return await interaction.response.send_message(
                embed=error_embed("Not Found"), ephemeral=True
            )

        gcfg = await self.bot.db.get_guild_settings(interaction.guild_id)
        view = ConfirmView(interaction.user.id, timeout=gcfg.confirm_timeout_seconds)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Remove Item",
                description=(
                    f"Remove **{item_name}** from the shop?\n"
                    "Users who already own it **will keep it in their inventory**."
                ),
                colour=discord.Colour.orange()
            ),
            view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        result = await self.bot.db.remove_shop_item(
            interaction.guild_id, interaction.user.id, item["item_id"]
        )
        if not result["ok"]:
            return await interaction.edit_original_response(
                embed=error_embed("Failed", result["error"]), view=None
            )

        await post_audit(
            self.bot, interaction.guild_id,
            executor=interaction.user,
            target="Shop",
            action="shop_item_remove",
            field=item_name,
        )
        await interaction.edit_original_response(
            embed=success_embed("Item Removed", f"**{item_name}** removed from shop."),
            view=None
        )

    # ── /shop iteminfo ─────────────────────────────────────────────────────────
    @shop.command(name="iteminfo", description="Get detailed info about a shop item.")
    @app_commands.describe(item_name="Name of the item")
    async def item_info(self, interaction: discord.Interaction, item_name: str):
        gid = interaction.guild_id
        gcfg = await self.bot.db.get_guild_settings(gid)
        item = await self.bot.db.get_shop_item_by_name(gid, item_name)
        if not item:
            if is_mod(interaction.user):
                item = await self.bot.db.get_shop_item_by_name(gid, item_name, include_deleted=True)
            if not item:
                return await interaction.response.send_message(
                    embed=error_embed("Not Found"), ephemeral=True
                )

        stock_str = "∞" if item["stock"] == -1 else str(item["stock"])
        limit_str = "∞" if item["max_per_user"] == -1 else str(item["max_per_user"])
        role_str = f"<@&{item['role_required']}>" if item["role_required"] else "None"
        deleted_str = "🗑️ **[DELETED FROM SHOP]**\n" if item["is_deleted"] else ""

        embed = info_embed(f"📦 Item Info: {item['name']}")
        embed.description = deleted_str + (item["description"] or "No description.")
        embed.add_field(name="Price",       value=gcfg.fmt_money(item["price"]), inline=True)
        embed.add_field(name="Stock",       value=stock_str,  inline=True)
        embed.add_field(name="Limit/User",  value=limit_str,  inline=True)
        embed.add_field(name="Role Required", value=role_str, inline=True)
        embed.add_field(name="Tradeable",   value="✅" if item["is_tradeable"] else "🚫", inline=True)
        if item["reply_message"]:
            embed.add_field(name="Purchase Reply", value=item["reply_message"], inline=False)
        embed.set_footer(text=f"Item ID: {item['item_id'][:8]}…")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /buy ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="buy", description="Buy an item from the shop.")
    @app_commands.describe(item_name="Name of the item to buy")
    async def buy(self, interaction: discord.Interaction, item_name: str):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id
        gcfg = await self.bot.db.get_guild_settings(gid)

        if await self.bot.db.check_rate_limit(gid, interaction.user.id, "buy", gcfg.rate_limit_seconds):
            return await interaction.followup.send(
                embed=error_embed("Slow down!"), ephemeral=True
            )

        item = await self.bot.db.get_shop_item_by_name(gid, item_name)
        if not item:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"No item called **{item_name}** in the shop."),
                ephemeral=True
            )

        if item["role_required"]:
            has_role = any(r.id == item["role_required"] for r in interaction.user.roles)
            if not has_role:
                return await interaction.followup.send(
                    embed=error_embed("Role Required", f"You need <@&{item['role_required']}> to buy this."),
                    ephemeral=True
                )

        result = await self.bot.db.purchase_item(gid, interaction.user.id, item["item_id"])
        await self.bot.db.update_rate_limit(gid, interaction.user.id, "buy")

        if not result["ok"]:
            return await interaction.followup.send(
                embed=error_embed("Purchase Failed", result["error"]), ephemeral=True
            )

        await post_audit(
            self.bot, gid,
            executor=interaction.user,
            target=interaction.user,
            action="shop_purchase",
            field=item_name,
            before=None,
            after=f"−{gcfg.fmt_money(item['price'])}",
            txn_id=result.get("txn_id"),
        )

        embed = success_embed(
            "Purchase Successful!",
            f"You bought **{item_name}** for {gcfg.fmt_money(item['price'])}."
        )
        if result.get("reply"):
            embed.add_field(name="📬 Message", value=result["reply"], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Shop(bot))
