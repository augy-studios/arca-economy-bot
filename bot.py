"""
bot.py — EconomyBot entry point
"""

import discord
from discord.ext import commands
import asyncio
import logging
import logging.handlers
import os
import sys
from utils.database import DatabaseManager
from utils.config import Config
from utils.helpers import post_alert


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    os.makedirs("logs", exist_ok=True)
    fh = logging.handlers.TimedRotatingFileHandler(
        "logs/bot.log", when="midnight", backupCount=7, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    eh = logging.FileHandler("logs/errors.log", encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    logger.addHandler(eh)
    return logging.getLogger("bot")


log = setup_logging()


class EconomyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db: DatabaseManager = None
        self.config = Config()

    async def setup_hook(self):
        self.db = DatabaseManager()
        await self.db.initialise()

        cogs = ["cogs.banking", "cogs.inventory", "cogs.shop", "cogs.admin", "cogs.help"]
        for cog in cogs:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded: {cog}")
            except Exception as e:
                log.error(f"Failed to load {cog}: {e}", exc_info=True)

        guild_id = self.config.GUILD_ID
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(f"Synced {len(synced)} slash commands to guild {guild_id}.")
        else:
            synced = await self.tree.sync()
            log.info(f"Synced {len(synced)} global slash commands.")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="the economy 💰"
            )
        )

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: discord.app_commands.AppCommandError):
        log.error(f"Slash command error in {interaction.command}: {error}", exc_info=True)
        msg = "An unexpected error occurred. Please try again."
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            msg = f"Slow down! Try again in {error.retry_after:.1f}s."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {msg}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        except Exception:
            pass
        await post_alert(self, interaction.guild_id, f"Command error in `{interaction.command}`: {error}")

    async def on_error(self, event_method: str, *args, **kwargs):
        log.error(f"Unhandled error in {event_method}", exc_info=True)
        await post_alert(self, None, f"Unhandled error in event `{event_method}`.")

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()


async def main():
    bot = EconomyBot()
    token = bot.config.TOKEN
    if not token:
        log.critical("BOT_TOKEN is not set in .env!")
        sys.exit(1)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
