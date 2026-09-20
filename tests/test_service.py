"""End-to-end smoke test of the service layer in dry-run mode (no Discord, no Google)."""
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from periods import SeasonState, current_season_state
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
        reconcile_minutes=15,
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


def test_season_end_clears_registrations(service):
    svc, db = service
    now = datetime.now(timezone.utc)
    # A season that already ended.
    past_start = (now - timedelta(days=30)).isoformat()
    svc.create_season("7d", 2, past_start)
    svc.register(FakeMember(9))
    assert db.is_any_participant()
    note = svc.cleanup_ended_season()
    assert note is not None and "cleared" in note.lower()
    assert not db.is_any_participant()
    # Idempotent: nothing left to clean.
    assert svc.cleanup_ended_season() is None


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
