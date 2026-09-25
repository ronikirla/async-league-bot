"""Entry point for the async speedrun league bot.

Usage:
    python main.py

Reads configuration from ``.env`` (see ``.env.example``), connects to
Discord, syncs the guild-scoped slash command tree, and restores the
season/round event timers (running any events that should have already
happened while the bot was offline).
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from cogs.league import League
from config import ConfigError, load_config
from db import Database
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
        # Ensure the League Admin role exists (with Administrator) and that
        # every configured admin user holds it. This is what makes the
        # /league_admin command group visible only to the admins.
        guild = self.get_guild(self.config.guild_id)
        if guild:
            try:
                for admin_id in self.config.admin_ids:
                    await self.roles.grant_admin(guild, discord.Object(id=admin_id))
            except Exception:
                log.exception("Failed to grant the League Admin role at startup")
        # Restore the exact event timers and run any season/round events that
        # should have already happened while the bot was offline.
        self.service.start_scheduler(lambda: self.get_guild(self.config.guild_id))


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
            await bot.start(config.token)
    finally:
        service.scheduler.stop()
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
