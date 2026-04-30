"""
cogs/help.py
Slash command: /help  — paginated command reference
"""

import discord
from discord import app_commands
from discord.ext import commands

# ── Page definitions ──

PAGES: list[dict] = [
    {
        "title": "📖 Arca Economy Bot — Help",
        "colour": discord.Colour.blurple(),
        "description": (
            "Use the buttons below to browse all commands.\n\n"
            "**Permission levels:**\n"
            "🌐 Everyone  |  🔧 Mod (Manage Channels)  |  🛡️ Admin (Manage Channels)\n\n"
            "**Categories**\n"
            "1️⃣ Banking — `/money`\n"
            "2️⃣ Inventory — `/inventory`\n"
            "3️⃣ Shop — `/shop`, `/buy`\n"
            "4️⃣ Trading — `/trade`\n"
            "5️⃣ Leaderboard & Audit Log\n"
            "6️⃣ Config — `/config`\n"
        ),
        "fields": [],
    },
    {
        "title": "💵 Banking — `/money`",
        "colour": discord.Colour.green(),
        "description": "Manage cash and bank wallets. Bulk operations (by role) show a confirmation prompt.",
        "fields": [
            {
                "name": "🌐 `/money balance [user]`",
                "value": "Check your own (or another user's) cash and bank balance.",
            },
            {
                "name": "🔧 `/money add <target> <amount> [field] [note]`",
                "value": (
                    "`target` — @user or @role\n"
                    "`field` — `cash` (default) or `bank`\n"
                    "Adds money to every non-bot member of the role, or a single user."
                ),
            },
            {
                "name": "🔧 `/money remove <target> <amount> [field] [note]`",
                "value": "Same as `add` but subtracts. Floors at 0 unless `allow_debt` is enabled.",
            },
            {
                "name": "🌐 `/money give <user> <amount>`",
                "value": (
                    "Gift cash from your wallet to another user.\n"
                    "Subject to gift cooldown, flood detection, and blacklist checks."
                ),
            },
            {
                "name": "🛡️ `/money reset <user>`",
                "value": "Reset a user's cash **and** bank to 0. Requires confirmation.",
            },
        ],
    },
    {
        "title": "🎒 Inventory — `/inventory`",
        "colour": discord.Colour.orange(),
        "description": "Items persist in inventories even after they are removed from the shop.",
        "fields": [
            {
                "name": "🌐 `/inventory view [user]`",
                "value": "Browse your own (or another user's) inventory.",
            },
            {
                "name": "🔧 `/inventory edit <action> <target> <item_name> [quantity] [note]`",
                "value": (
                    "`action` — `Add` or `Remove`\n"
                    "`target` — @user or @role\n"
                    "`quantity` — defaults to 1\n"
                    "Bulk role edits require confirmation."
                ),
            },
            {
                "name": "🌐 `/inventory give <user> <item_name> [quantity]`",
                "value": (
                    "Transfer an item from your inventory to another user.\n"
                    "Non-tradeable items are blocked. Subject to gift cooldown."
                ),
            },
        ],
    },
    {
        "title": "🛒 Shop — `/shop` & `/buy`",
        "colour": discord.Colour.gold(),
        "description": "Full shop management. Deleted items survive in existing inventories.",
        "fields": [
            {
                "name": "🌐 `/shop view`",
                "value": "Browse all active shop items with prices, stock, and tradeability.",
            },
            {
                "name": "🌐 `/shop iteminfo <item_name>`",
                "value": "Detailed info: stock, per-user limit, role gate, purchase reply, item ID.",
            },
            {
                "name": "🌐 `/buy <item_name>`",
                "value": (
                    "Purchase an item using your cash balance.\n"
                    "Respects role gate, stock, per-user limit, and rate limit."
                ),
            },
            {
                "name": "🔧 `/shop add <name> <price> [description] [stock] [max_per_user] [role_required] [reply_message] [tradeable]`",
                "value": (
                    "`stock` / `max_per_user` — `-1` = unlimited\n"
                    "`role_required` — role ID or @mention\n"
                    "`tradeable` — whether users can trade/gift this item (default `true`)"
                ),
            },
            {
                "name": "🔧 `/shop edit <item_name> [new_name] [price] [description] [stock] [max_per_user] [reply_message] [tradeable]`",
                "value": "Edit any field of an existing item. Only provided fields are changed.",
            },
            {
                "name": "🔧 `/shop remove <item_name>`",
                "value": "Soft-delete an item from the shop. Existing owners keep their copies.",
            },
        ],
    },
    {
        "title": "🔄 Trading — `/trade`",
        "colour": discord.Colour.teal(),
        "description": "Escrow-based peer-to-peer trades. Both cash and items can be exchanged at once.",
        "fields": [
            {
                "name": "🌐 `/trade <user> [offer_cash] [offer_item] [offer_qty] [request_cash] [request_item] [request_qty]`",
                "value": (
                    "Sends an Accept / Decline prompt to the target user.\n"
                    "**offer_*** — what you put in  |  **request_*** — what you want back\n"
                    "Trade expires after `trade_timeout_seconds` (default 120 s).\n"
                    "Non-tradeable items are blocked from trades."
                ),
            },
        ],
    },
    {
        "title": "🏆 Leaderboard & 📋 Audit Log",
        "colour": discord.Colour.purple(),
        "description": "",
        "fields": [
            {
                "name": "🌐 `/leaderboard [category]`",
                "value": (
                    "Top-20 rankings. Categories:\n"
                    "💵 Cash · 🏦 Bank · 💰 Total Wealth · 💸 Most Spent · 🎒 Most Items\n"
                    "Cache refreshes every `lb_cache_ttl` seconds (default 300 s)."
                ),
            },
            {
                "name": "🔧 `/auditlog [user] [page]`",
                "value": (
                    "Paginated audit log (10 entries per page).\n"
                    "Filter by user, or leave blank for the full server log.\n"
                    "Each entry shows executor, target, before/after values, and a TXN ID."
                ),
            },
        ],
    },
    {
        "title": "🔧 Config — `/config`",
        "colour": discord.Colour.red(),
        "description": "All config commands are ephemeral and require the **Manage Channels** permission.",
        "fields": [
            {
                "name": "🛡️ `/config set <key> <value>`",
                "value": (
                    "Set a bot configuration value. Available keys:\n"
                    "`audit_log_channel` · `alert_channel` · `currency_symbol` · `currency_name`\n"
                    "`max_balance` · `max_daily_earn` · `allow_debt`\n"
                    "`gift_cooldown_hours` · `gift_flagging_threshold` · `gift_flagging_window_hours`\n"
                    "`rate_limit_seconds` · `lb_cache_ttl` · `confirm_timeout_seconds` · `trade_timeout_seconds`"
                ),
            },
            {
                "name": "🛡️ `/config view`",
                "value": "Display all current configuration values for this server.",
            },
            {
                "name": "🛡️ `/config backup`",
                "value": "Manually trigger a database backup (auto-runs every 6 h, keeps last 28).",
            },
            {
                "name": "🛡️ `/config integrity`",
                "value": "Run the database integrity scan immediately (auto-runs every 6 h). Fixes negative balances and flags anomalies.",
            },
            {
                "name": "🛡️ `/config blacklist_add <user> [reason]`",
                "value": "Block a user from receiving gifts or items.",
            },
            {
                "name": "🛡️ `/config blacklist_remove <user>`",
                "value": "Remove a user from the gift/item blacklist.",
            },
            {
                "name": "🛡️ `/config refreshlb`",
                "value": "Force an immediate leaderboard cache rebuild.",
            },
        ],
    },
]

TOTAL_PAGES = len(PAGES)


def _build_embed(page_index: int) -> discord.Embed:
    page = PAGES[page_index]
    embed = discord.Embed(
        title=page["title"],
        description=page.get("description", ""),
        colour=page["colour"],
    )
    for field in page.get("fields", []):
        embed.add_field(name=field["name"], value=field["value"], inline=False)
    embed.set_footer(text=f"Page {page_index + 1} / {TOTAL_PAGES}")
    return embed


# ── Paginator view ──

class HelpView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.first_btn.disabled = self.page == 0
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == TOTAL_PAGES - 1
        self.last_btn.disabled = self.page == TOTAL_PAGES - 1

    async def _go_to(self, interaction: discord.Interaction, new_page: int):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message(
                "Only the user who ran `/help` can navigate.", ephemeral=False
            )
        self.page = new_page
        self._update_buttons()
        await interaction.response.edit_message(embed=_build_embed(self.page), view=self)

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._go_to(interaction, 0)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._go_to(interaction, self.page - 1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._go_to(interaction, self.page + 1)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._go_to(interaction, TOTAL_PAGES - 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ──

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Browse all bot commands with navigation buttons.")
    async def help_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = HelpView(interaction.user.id)
        await interaction.followup.send(embed=_build_embed(0), view=view)


async def setup(bot):
    await bot.add_cog(Help(bot))
