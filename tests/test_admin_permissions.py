"""Tests for the /league_admin group: visibility + access control."""
from __future__ import annotations

import asyncio

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from cogs.league import League
from config import Config
from db import Database
from roles import ADMIN_GROUP_PERMISSION, ADMIN_ROLE_NAME, RoleManager
from service import LeagueService
from sheets import SheetService


class FakeRole:
    def __init__(self, name: str, rid: int = 100):
        self.name = name
        self.id = rid


class FakeMember(discord.Member):
    def __init__(self, mid: int, roles: list):
        self._mid = mid
        self._roles = list(roles)

    @property
    def id(self):
        return self._mid

    @property
    def roles(self):
        return self._roles

    @property
    def guild_permissions(self):
        if any(r.name == ADMIN_ROLE_NAME for r in self._roles):
            return discord.Permissions.all()
        return discord.Permissions.none()


@pytest.fixture
def bot(tmp_path):
    import os

    db = Database(os.path.join(tmp_path, "t.db"))
    cfg = Config("x", 1, (1,), "x", "x", "P", "S", True)
    roles = RoleManager(cfg, db)
    sheets = SheetService(cfg, db)
    svc = LeagueService(cfg, db, roles, sheets)

    import main

    b = main.LeagueBot(cfg, db, roles, sheets, svc)
    yield b, cfg
    db.close()


def test_admin_group_hidden_from_non_admins(bot):
    """The /league_admin group must serialize with Administrator-only
    default_member_permissions so Discord hides it from regular members."""
    b, cfg = bot

    async def run():
        await b.add_cog(League(b))
        guild = discord.Object(id=cfg.guild_id)
        b.tree.copy_global_to(guild=guild)
        admin_group = b.tree.get_command("league_admin")
        public_group = b.tree.get_command("league")
        assert admin_group is not None
        assert public_group is not None
        # The admin group must be gated by the shared admin permission.
        assert admin_group.default_permissions == ADMIN_GROUP_PERMISSION
        # The public group must NOT restrict visibility.
        assert public_group.default_permissions is None
        # Public group still holds the participant commands.
        public_names = {c.name for c in public_group.commands}
        assert {"register", "unregister", "seed", "submit"} <= public_names
        # Admin group holds the admin commands.
        admin_names = {c.name for c in admin_group.commands}
        assert {"setup", "create_season", "season_info",
                "add_participant", "remove_participant"} <= admin_names

    asyncio.run(run())


def test_admin_only_predicate(bot):
    """The in-code admin gate accepts configured ids AND League Admin role
    holders, and rejects everyone else."""
    b, cfg = bot
    admin_role = FakeRole(ADMIN_ROLE_NAME)

    class FakeResponse:
        def __init__(self):
            self.sent = []

        async def send_message(self, text, ephemeral=False):
            self.sent.append(text)

    def make_interaction(user_id: int, roles: list):
        guild = type("G", (), {
            "get_member": lambda self, uid: FakeMember(uid, roles) if uid in (1, 2) else None,
        })()
        interaction = type("I", (), {
            "client": b,
            "user": discord.Object(id=user_id),
            "guild": guild,
            "response": FakeResponse(),
        })()
        return interaction

    # The check is the first (and only) check on any admin command.
    import main as _  # noqa: F401  (ensure League imports resolve)

    async def run():
        await b.add_cog(League(b))
        guild = discord.Object(id=cfg.guild_id)
        b.tree.copy_global_to(guild=guild)
        cmd = b.tree.get_command("league_admin").get_command("season_info")
        assert len(cmd.checks) == 1
        check = cmd.checks[0]

        # Configured admin id (1) -> allowed even without the role.
        i = make_interaction(1, [])
        assert await check(i) is True
        assert i.response.sent == []

        # Role holder (2) not in ADMIN_IDS -> allowed.
        i = make_interaction(2, [admin_role])
        assert await check(i) is True

        # Stranger without role -> denied + friendly message.
        i = make_interaction(3, [])
        assert await check(i) is False
        assert len(i.response.sent) == 1

    asyncio.run(run())
