"""Regression tests for the role reconciliation path (uses a real SeasonState)."""
from datetime import datetime, timedelta, timezone

import discord
import pytest

from config import Config
from db import Database
from periods import current_season_state
from roles import RoleManager


class FakeRole:
    def __init__(self, name: str, rid: int = 100):
        self.name = name
        self.id = rid


class FakeMember(discord.Member):
    """Minimal discord.Member stand-in (passes isinstance checks in reconcile).

    ``id`` and ``roles`` are read-only properties on the real class, so they
    are shadowed here with properties backed by private attributes.
    """

    def __init__(self, mid: int, roles: list):
        self._mid = mid
        self._roles = list(roles)
        self.added = []
        self.removed = []

    @property
    def id(self):
        return self._mid

    @property
    def roles(self):
        return self._roles

    async def add_roles(self, role, reason=None):
        self.added.append(role)
        self._roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.removed.append(role)
        if role in self._roles:
            self._roles.remove(role)

    def __eq__(self, other):
        return isinstance(other, FakeMember) and self._mid == other._mid

    def __hash__(self):
        return hash(self._mid)


class FakeGuild:
    def __init__(self, members: list, roles: list):
        self.members = members
        self.roles = roles


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    cfg = Config("x", 1, (1,), "x", "x", "P", "S", True, 15)
    rm = RoleManager(cfg, db)
    yield db, rm
    db.close()


def test_reconcile_with_real_state(env):
    """Regression: reconcile must pass the integer season id to the DB layer."""
    import asyncio

    db, rm = env
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=1)).isoformat()
    season_id = db.create_season(7 * 86400, 3, start)
    db.add_participant(1, "a")
    db.add_participant(2, "b")
    for pid in (1, 2):
        db.create_record(season_id, 1, pid)
    db.mark_submitted(season_id, 1, 2, 600.0, "https://youtu.be/x")

    state = current_season_state(db)
    assert state is not None and state.in_season

    not_done_role = FakeRole("S")
    member1 = FakeMember(1, [])                 # unsubmitted -> should gain the role
    member2 = FakeMember(2, [not_done_role])    # submitted   -> should lose the role
    guild = FakeGuild(members=[member1, member2], roles=[FakeRole("P"), not_done_role])

    # This used to crash with:
    #   sqlite3.ProgrammingError: Error binding parameter 1: type 'SeasonSpec'
    asyncio.run(rm.reconcile(guild, state))

    assert not_done_role in member1.added
    assert not_done_role in member2.removed


def test_reconcile_no_state(env):
    """Without an active season, everyone loses the seed-not-done role."""
    import asyncio

    db, rm = env
    not_done_role = FakeRole("S")
    member1 = FakeMember(1, [not_done_role])
    guild = FakeGuild(members=[member1], roles=[FakeRole("P"), not_done_role])

    asyncio.run(rm.reconcile(guild, None))

    assert not_done_role in member1.removed
    assert not member1.added
