"""End-to-end smoke test of the service layer in dry-run mode (no Discord, no Google)."""
import asyncio
from datetime import datetime, timedelta, timezone

import discord
import pytest

from config import Config
from db import Database
from roles import RoleManager
from service import LeagueError, LeagueService
from sheets import SheetService, DRY_RUN_LOG_FILE


class FakeMember:
    def __init__(self, user_id: int, name: str = "runner"):
        self.id = user_id
        self.global_name = name
        self.name = name
        self.mention = f"<@{user_id}>"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep the dry-run log file inside the temp dir
    db = Database(str(tmp_path / "league.db"))
    config = Config(
        token="x",
        guild_id=1,
        admin_ids=(1,),
        spreadsheet_id="x",
        service_account_file="x",
        participant_role_name="League Participant",
        seed_not_done_role_name="League Seed Not Done",
        dry_run=True,
    )
    roles = RoleManager(config, db)
    sheets = SheetService(config, db)
    svc = LeagueService(config, db, roles, sheets)
    yield svc, db
    db.close()


def test_full_period_flow(service, tmp_path):
    svc, db = service

    # No season yet.
    with pytest.raises(LeagueError):
        svc.request_seed(FakeMember(2))

    # Create a season that starts in the future (registration is open).
    now = datetime.now(timezone.utc)
    upcoming_start = (now + timedelta(hours=1)).isoformat()
    svc.create_season("7d", 3, upcoming_start)

    # Registration is open before the season starts.
    assert "Registered" in svc.register(FakeMember(2, "alpha"))
    assert "already" in svc.register(FakeMember(2, "alpha"))
    svc.register(FakeMember(3, "beta"))

    # Create a second season starting 1 day in the past (active period 1).
    past_start = (now - timedelta(days=1)).isoformat()
    state = svc.create_season("7d", 3, past_start)
    assert state.in_season
    assert state.period_index == 1

    # Registration closes once a period is active.
    with pytest.raises(LeagueError) as exc:
        svc.register(FakeMember(4, "late"))
    assert "closed" in str(exc.value).lower()
    assert not db.is_participant(4)

    # Non-participant cannot request the seed.
    with pytest.raises(LeagueError):
        svc.request_seed(FakeMember(4))

    # First seed request generates + records the seed; second call is idempotent.
    r1 = svc.request_seed(FakeMember(2))
    assert 0 <= int(r1.seed) <= 9999999999
    assert r1.first_request is True
    r2 = svc.request_seed(FakeMember(2))
    assert r2.seed == r1.seed
    assert r2.first_request is False

    # Beta requests the same period seed.
    r3 = svc.request_seed(FakeMember(3))
    assert r3.seed == r1.seed

    # Submission validation.
    with pytest.raises(LeagueError):
        svc.submit(FakeMember(2), "not-a-time", "https://youtu.be/x")
    with pytest.raises(LeagueError):
        svc.submit(FakeMember(2), "12:34", "not-a-url")

    # Valid submission.
    result = svc.submit(FakeMember(2), "12:34.567", "https://youtu.be/abc")
    assert result.run_time == "12:34.567"

    # The run time is stored numerically (seconds) in the database.
    rec = db.record_exists(state.season.season_id, 1, 2)
    assert float(rec["run_time"]) == pytest.approx(754.567)

    # Double submission rejected.
    with pytest.raises(LeagueError):
        svc.submit(FakeMember(2), "10:00.000", "https://youtu.be/abc")

    # DB state matches.
    season_id = state.season.season_id
    assert db.has_submitted(season_id, 1, 2)
    assert not db.has_submitted(season_id, 1, 3)
    assert db.unsubmitted_ids(season_id, 1) == {3}
    assert db.get_period_seed(season_id, 1) == r1.seed

    # Season info mentions the active round and seed.
    info = svc.season_info()
    assert "Active round: **1/3**" in info
    assert r1.seed in info

    # Dry-run log captured the sheet writes.
    import os
    assert os.path.exists(DRY_RUN_LOG_FILE)


def test_dnf_flow(service, tmp_path):
    """A runner who did not finish can mark the round DNF."""
    svc, db = service
    now = datetime.now(timezone.utc)
    # Registration is closed during an active round, so use the admin path.
    state = svc.create_season("7d", 3, (now - timedelta(days=1)).isoformat())
    svc.add_participant(FakeMember(5, "dnfer"))

    result = svc.dnf(FakeMember(5))
    assert result.run_time == "DNF"
    assert db.has_dnf(state.season.season_id, 1, 5)
    # A DNF counts as done: role reconciliation treats them as not needing the role.
    assert 5 not in db.unsubmitted_ids(state.season.season_id, 1)

    # DNF again -> rejected.
    with pytest.raises(LeagueError):
        svc.dnf(FakeMember(5))

    # A submitted runner cannot DNF, and a DNF'd runner cannot submit.
    svc.add_participant(FakeMember(6, "submitter"))
    svc.submit(FakeMember(6), "10:00.000", "https://youtu.be/x")
    with pytest.raises(LeagueError):
        svc.dnf(FakeMember(6))
    svc.add_participant(FakeMember(7, "dnfer2"))
    svc.dnf(FakeMember(7))
    with pytest.raises(LeagueError):
        svc.submit(FakeMember(7), "10:00.000", "https://youtu.be/x")


def test_season_boundary(service):
    svc, db = service
    now = datetime.now(timezone.utc)
    # Season ended long ago.
    start = (now - timedelta(days=30)).isoformat()
    state = svc.create_season("7d", 2, start)
    assert not state.in_season
    svc.register(FakeMember(2))
    with pytest.raises(LeagueError) as exc:
        svc.request_seed(FakeMember(2))
    assert "ended" in str(exc.value).lower()


def test_add_and_remove_participant(service):
    svc, db = service
    msg = svc.add_participant(FakeMember(7, "gamma"))
    assert "Registered" in msg
    assert db.is_participant(7)
    msg = svc.add_participant(FakeMember(7, "gamma"))
    assert "already" in msg
    assert "Removed" in svc.remove_participant(FakeMember(7, "gamma"))
    assert not db.is_participant(7)
    assert "not a registered participant" in svc.remove_participant(FakeMember(7))


def test_unregister(service):
    svc, db = service
    svc.register(FakeMember(5))
    assert db.is_participant(5)
    assert "unregistered" in svc.unregister(FakeMember(5)).lower()
    assert not db.is_participant(5)
    # Unregistering again is a no-op.
    assert "not registered" in svc.unregister(FakeMember(5)).lower()


def test_season_end_event_clears_registrations(service):
    svc, db = service
    now = datetime.now(timezone.utc)
    # A season that already ended.
    past_start = (now - timedelta(days=30)).isoformat()
    state = svc.create_season("7d", 2, past_start)
    svc.register(FakeMember(9))
    assert db.is_any_participant()

    from scheduler import ScheduledEvent

    async def run():
        guild = type("G", (), {"roles": [], "members": []})()
        await svc.on_scheduled_event(
            guild, ScheduledEvent("season_end", now, None, state.season.season_id)
        )

    asyncio.run(run())

    assert not db.is_any_participant()
    # Idempotent: replaying the event clears nothing (and does not crash).
    asyncio.run(run())
    assert not db.is_any_participant()


def test_boot_catchup_posts_missed_announcements(service):
    """Boot with a season already in progress: every missed event is
    replayed through the real dispatcher and announced in the channel."""
    svc, db = service
    now = datetime.now(timezone.utc)
    # 10 days into a 14-day season: rounds 1 start/end and round 2 start
    # should have happened already.
    svc.create_season("7d", 2, (now - timedelta(days=10)).isoformat())
    # Registration is closed mid-season, so add the participants directly.
    db.add_participant(21, "alpha")
    db.add_participant(22, "beta")

    class FakeRole:
        id = 555
        name = "League Seed Not Done"

        @property
        def mention(self):
            return "<@&555>"

    class FakeChannel(discord.TextChannel):
        id = 777
        name = "announcements"

        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)

    class FakeGuild:
        id = 1
        roles = [FakeRole()]
        members = []
        system_channel = None

        def get_channel(self, cid):
            return channel if cid == channel.id else None

        async def create_role(self, **kwargs):
            return FakeRole()

    channel = FakeChannel()
    guild = FakeGuild()
    db.set_setting("announce_channel_id", "777")

    async def run():
        svc.start_scheduler(lambda: guild)
        await asyncio.wait_for(svc.scheduler._queue.join(), timeout=5)
        svc.scheduler.stop()

    asyncio.run(run())

    text = "\n".join(channel.sent)
    # Round 1's start doubles as the season-start announcement.
    assert "Season 1, Round 1/2 has started" in text
    assert "Registration is closed while the season runs." in text
    assert "Season 1, Round 2/2 has started" in text
    # The 24h reminder for round 1 pinged the seed-not-done role.
    assert "24 hours left" in text
    assert "<@&555>" in text
    # Round ends are not announced at all (only the season end is, and it
    # is still future here).
    assert "has ended" not in text


def test_create_season_via_to_thread_arms_timers(service):
    """Regression for the production crash: /create_season runs via
    ``asyncio.to_thread`` while the scheduler is already started; the timer
    arming must be handed to the event loop (was raising
    ``RuntimeError: no running event loop``)."""
    svc, db = service
    now = datetime.now(timezone.utc)

    async def run():
        svc.start_scheduler(lambda: None)  # guild resolver; no events due
        await asyncio.to_thread(
            svc.create_season, "7d", 1, (now + timedelta(hours=1)).isoformat()
        )
        await asyncio.sleep(0.05)  # let the loop-queued reschedule run
        pending = set(svc.scheduler.pending())
        svc.scheduler.stop()
        return pending

    pending = asyncio.run(run())

    # 7d round starting in 1h: no round-end events; three future timers.
    assert pending == {
        ("period_start", 1),
        ("period_reminder", 1),
        ("season_end", None),
    }


def test_signups_announced_now_and_season_start_on_timer(service):
    """A season created with a future start announces sign-ups immediately;
    the season/round-start announcements fire when the timer comes due."""
    svc, db = service
    now = datetime.now(timezone.utc)

    class FakeRole:
        id = 555
        name = "League Seed Not Done"

        @property
        def mention(self):
            return "<@&555>"

    class FakeChannel(discord.TextChannel):
        id = 777
        name = "general"

        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)

    class FakeGuild:
        id = 1
        roles = [FakeRole()]
        members = []
        system_channel = None

        def get_channel(self, cid):
            return channel if cid == channel.id else None

        async def create_role(self, **kwargs):
            return FakeRole()

    channel = FakeChannel()
    guild = FakeGuild()
    db.set_setting("announce_channel_id", "777")

    async def run():
        svc.start_scheduler(lambda: guild)
        state = await asyncio.to_thread(
            svc.create_season, "7d", 1, (now + timedelta(seconds=1.5)).isoformat()
        )
        # The cog does this right after create_season:
        posted = await svc.announce_signups_open(guild, state)
        # Sign-ups are posted synchronously, before any timer has fired.
        first = channel.sent[0] if channel.sent else ""
        await asyncio.sleep(2.0)  # the 1.5s-out season-start timer fires
        svc.scheduler.stop()
        return posted, first, list(channel.sent)

    posted, first, after = asyncio.run(run())

    assert posted is True
    assert "Sign-ups are open for Season 1" in first
    # The round length is shown in the compact form (no timedelta repr).
    assert "Round length: 7d" in first
    assert "0:00:00" not in first
    text = "\n".join(after)
    assert "Sign-ups are open for Season 1" in text
    # Round 1's start doubles as the season-start announcement.
    assert "Season 1, Round 1/1 has started" in text
    assert "Registration is closed while the season runs." in text
    assert "has ended" not in text  # the season end is still 7d out


def test_restart_does_not_repost_handled_events(service):
    """Regression: catch-up on a restart must skip events that were already
    dispatched before the restart, instead of re-posting them."""
    svc, db = service
    now = datetime.now(timezone.utc)
    # 10 days into a 14-day season: round 1 start, its 24h reminder and
    # round 2 start are all in the past.
    svc.create_season("7d", 2, (now - timedelta(days=10)).isoformat())
    db.add_participant(31, "alpha")

    class FakeRole:
        id = 555
        name = "League Seed Not Done"

        @property
        def mention(self):
            return "<@&555>"

    class FakeChannel(discord.TextChannel):
        id = 777
        name = "general"

        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)

    class FakeGuild:
        id = 1
        roles = [FakeRole()]
        members = []
        system_channel = None

        def get_channel(self, cid):
            return channel if cid == channel.id else None

        async def create_role(self, **kwargs):
            return FakeRole()

    channel = FakeChannel()
    guild = FakeGuild()
    db.set_setting("announce_channel_id", "777")

    async def boot():
        svc.start_scheduler(lambda: guild)
        await asyncio.wait_for(svc.scheduler._queue.join(), timeout=5)
        pending = set(svc.scheduler.pending())
        svc.scheduler.stop()
        return pending

    asyncio.run(boot())
    first_boot = list(channel.sent)
    assert len(first_boot) == 3  # round 1 start + its reminder + round 2 start

    # A second boot right after: nothing new is missed, nothing re-posted,
    # and the still-future events are armed as timers again.
    second_pending = asyncio.run(boot())
    assert channel.sent == first_boot
    assert second_pending == {("period_reminder", 2), ("season_end", None)}


def test_failed_announcements_retry_on_next_boot(service):
    """An event whose announcement could not be posted (e.g. no announce
    channel configured yet) is NOT marked as dispatched, so the next boot
    retries it instead of silently dropping it."""
    svc, db = service
    now = datetime.now(timezone.utc)
    # Round 1 of a single-round season started 2 days ago.
    svc.create_season("7d", 1, (now - timedelta(days=2)).isoformat())
    db.add_participant(41, "alpha")

    class FakeChannel(discord.TextChannel):
        id = 777
        name = "general"

        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)

    class FakeGuild:
        id = 1
        roles = []
        members = []
        system_channel = None

        def get_channel(self, cid):
            return channel if cid == channel.id else None

    channel = FakeChannel()
    guild = FakeGuild()

    async def boot():
        svc.start_scheduler(lambda: guild)
        await asyncio.wait_for(svc.scheduler._queue.join(), timeout=5)
        svc.scheduler.stop()

    asyncio.run(boot())  # no announce channel configured yet
    assert channel.sent == []
    assert not db.is_event_dispatched(1, "period_start", 1)

    db.set_setting("announce_channel_id", "777")
    asyncio.run(boot())  # channel configured now: catch-up delivers
    text = "\n".join(channel.sent)
    assert "Round 1/1 has started" in text
    assert db.is_event_dispatched(1, "period_start", 1)


def test_registration_closed_during_active_period(service):
    svc, db = service
    now = datetime.now(timezone.utc)
    # Register before the season starts.
    upcoming = (now + timedelta(hours=1)).isoformat()
    svc.create_season("7d", 1, upcoming)
    svc.register(FakeMember(10))
    # Now an active season exists.
    past_start = (now - timedelta(days=1)).isoformat()
    svc.create_season("7d", 1, past_start)
    with pytest.raises(LeagueError):
        svc.unregister(FakeMember(10))
    # ...and admin add is unaffected (no registration gate).
    assert db.is_participant(10)
