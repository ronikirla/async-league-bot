"""Entry point for the async speedrun league bot.

Usage:
    python main.py

Reads configuration from ``.env`` (see ``.env.example``), connects to
Discord, syncs the guild-scoped slash command tree, and starts a periodic
role reconciliation loop.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from cogs.league import League
from config import ConfigError, load_config
from db import Database
from periods import current_season_state
from roles import RoleManager
from service import LeagueService
from sheets import SheetService

log = logging.getLogger("league")

class LeagueBot(commands.Bot):
    def __init__(self, config, db: Database, roles: RoleManager,
                 sheets: SheetService, service: LeagueService):
        intents = discord.Intents.default()
        intents.members = True  # needed to read/manage member roles
        # command_prefix=None: this bot only uses slash commands.
        super().__init__(command_prefix=None, intents=intents)
        self.config = config
        self.db = db
        self.roles = roles
        self.sheets = sheets
        self.service = service

    async def on_message(self, message: discord.Message) -> None:
        # This bot is slash-command-only; the message-command pipeline is
        # disabled (with command_prefix=None it would raise on every message).
        return

    async def setup_hook(self) -> None:
        await self.add_cog(League(self))
        # Guild-scoped commands appear instantly (no global sync delay).
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced for guild %s", self.config.guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)


async def reconcile_loop(bot: LeagueBot) -> None:
    """Periodically re-sync the 'seed not done' role with the database.

    Also clears all registrations (and their roles) once a season ends.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(bot.config.guild_id)
            if guild:
                # A season may have ended: wipe registrations and strip roles.
                if bot.service.cleanup_ended_season() is not None:
                    await bot.roles.strip_league_roles(guild)
                state = current_season_state(bot.db)
                await bot.roles.reconcile(guild, state)
        except Exception:
            log.exception("Role reconciliation failed")
        await asyncio.sleep(bot.config.reconcile_minutes * 60)


async def run() -> None:
    config = load_config()
    db = Database()
    roles = RoleManager(config, db)
    sheets = SheetService(config, db)
    service = LeagueService(config, db, roles, sheets)
    sheets.verify()

    bot = LeagueBot(config, db, roles, sheets, service)
    try:
        async with bot:
            bot.loop.create_task(reconcile_loop(bot))
            await bot.start(config.token)
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")
    except discord.LoginFailure:
        raise SystemExit("Discord login failed. Check DISCORD_TOKEN in .env.")


if __name__ == "__main__":
    main()
