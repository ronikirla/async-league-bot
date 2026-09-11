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

    # Create a season starting in the past (1 day ago), 1-week periods, 3 periods.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=1)).isoformat()
    state = svc.create_season("7d", 3, start)
    assert state.in_season
    assert state.period_index == 1

    # Non-participant cannot request the seed.
    with pytest.raises(LeagueError):
        svc.request_seed(FakeMember(2))

    # Register two participants.
    assert "Registered" in svc.register(FakeMember(2, "alpha"))
    assert "already" in svc.register(FakeMember(2, "alpha"))
    svc.register(FakeMember(3, "beta"))

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

    # Double submission rejected.
    with pytest.raises(LeagueError):
        svc.submit(FakeMember(2), "10:00.000", "https://youtu.be/abc")

    # DB state matches.
    season_id = state.season.season_id
    assert db.has_submitted(season_id, 1, 2)
    assert not db.has_submitted(season_id, 1, 3)
    assert db.unsubmitted_ids(season_id, 1) == {3}
    assert db.get_period_seed(season_id, 1) == r1.seed

    # Season info mentions the active period and seed.
    info = svc.season_info()
    assert "Active period: **1/3**" in info
    assert r1.seed in info

    # Dry-run log captured the sheet writes.
    assert tmp_path is not None  # dry-run log is written to the CWD file
    import os
    assert os.path.exists(DRY_RUN_LOG_FILE)


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
