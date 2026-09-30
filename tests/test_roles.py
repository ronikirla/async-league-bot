"""Regression tests for the role reconciliation path (uses a real SeasonState)."""
from datetime import datetime, timedelta, timezone

import discord
import pytest

from config import Config
from db import Database
from periods import SeasonSpec
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

    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles)
        self._roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self.removed.extend(roles)
        for role in roles:
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
    cfg = Config("x", 1, (1,), "x", "x", "P", "S", True)
    rm = RoleManager(cfg, db)
    yield db, rm
    db.close()


def test_round_start_grants_role_to_unsubmitted(env):
    """Round start grants the seed-not-done role to participants who have
    not reported the new round, and removes it from non-participants."""
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

    not_done_role = FakeRole("S")
    member1 = FakeMember(1, [FakeRole("P")])                 # unsubmitted -> gains the role
    member2 = FakeMember(2, [FakeRole("P"), not_done_role])  # submitted   -> loses the role
    guild = FakeGuild(members=[member1, member2], roles=[FakeRole("P"), not_done_role])

    spec = SeasonSpec(season_id=season_id, start_at=datetime.fromisoformat(start),
                      period_length=timedelta(days=7), num_periods=3)
    asyncio.run(rm.on_round_start(guild, spec, 1))

    assert not_done_role in member1.added
    assert not_done_role in member2.removed


def test_season_end_strips_league_roles(env):
    """Season end removes BOTH league roles from every holder (idempotent)."""
    import asyncio

    db, rm = env
    participant_role = FakeRole("P")
    not_done_role = FakeRole("S")
    member1 = FakeMember(1, [participant_role, not_done_role])
    member2 = FakeMember(2, [participant_role])
    member3 = FakeMember(3, [FakeRole("Unrelated")])
    guild = FakeGuild(
        members=[member1, member2, member3],
        roles=[participant_role, not_done_role, FakeRole("Unrelated")],
    )

    asyncio.run(rm.strip_league_roles(guild))

    assert participant_role in member1.removed
    assert not_done_role in member1.removed
    assert participant_role in member2.removed
    assert not member3.removed
    # Idempotent: a second pass removes nothing new.
    asyncio.run(rm.strip_league_roles(guild))
    assert member1.removed.count(participant_role) == 1
    assert member1.removed.count(not_done_role) == 1


def test_round_start_is_noop_without_setup(env):
    """Round start (and the season-end strip) do nothing when /setup has not
    created the roles."""
    import asyncio

    db, rm = env
    member1 = FakeMember(1, [])
    guild = FakeGuild(members=[member1], roles=[FakeRole("P")])
    spec = SeasonSpec(season_id=1, start_at=datetime.now(timezone.utc),
                      period_length=timedelta(days=7), num_periods=1)

    asyncio.run(rm.on_round_start(guild, spec, 1))
    # Season-end strip is also a no-op when the roles do not exist.
    asyncio.run(rm.strip_league_roles(guild))

    assert not member1.added
    assert not member1.removed
