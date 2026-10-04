"""Unit tests for the SQLite persistence layer."""
import pytest

from db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


def test_participant_roundtrip(db):
    assert db.add_participant(111, "alpha") is True
    assert db.add_participant(111, "alpha again") is False
    assert db.is_participant(111)
    assert not db.is_participant(222)
    row = db.get_participant(111)
    assert row["display_name"] == "alpha"
    db.remove_participant(111)
    assert not db.is_participant(111)


def test_record_first_request_wins(db):
    db.add_participant(1, "a")
    season_id = db.create_season(86400, 2, "2026-09-10T00:00:00")
    db.create_record(season_id, 1, 1)
    assert db.mark_seed_requested(season_id, 1, 1) is True
    assert db.mark_seed_requested(season_id, 1, 1) is False  # second call ignored


def test_seed_is_shared_and_idempotent(db):
    db.add_participant(1, "a")
    db.add_participant(2, "b")
    season_id = db.create_season(86400, 2, "2026-09-10T00:00:00")
    db.create_record(season_id, 1, 1)
    db.create_record(season_id, 1, 2)
    assert db.get_period_seed(season_id, 1) is None
    db.set_record_seed(season_id, 1, "42")
    assert db.get_period_seed(season_id, 1) == "42"
    db.set_record_seed(season_id, 1, "43")  # must not overwrite
    assert db.get_period_seed(season_id, 1) == "42"


def test_submission_is_final(db):
    db.add_participant(1, "a")
    season_id = db.create_season(86400, 2, "2026-09-10T00:00:00")
    db.create_record(season_id, 1, 1)
    assert not db.has_submitted(season_id, 1, 1)
    assert db.mark_submitted(season_id, 1, 1, 600.0, "https://youtu.be/x") is True
    assert db.has_submitted(season_id, 1, 1)
    assert db.mark_submitted(season_id, 1, 1, 599.0, "https://youtu.be/y") is False
    # Other period is independent.
    db.create_record(season_id, 2, 1)
    assert not db.has_submitted(season_id, 2, 1)


def test_unsubmitted_ids(db):
    db.add_participant(1, "a")
    db.add_participant(2, "b")
    db.add_participant(3, "c")
    season_id = db.create_season(86400, 2, "2026-09-10T00:00:00")
    for pid in (1, 2, 3):
        db.create_record(season_id, 1, pid)
    db.mark_submitted(season_id, 1, 2, 600.0, "https://youtu.be/x")
    assert db.unsubmitted_ids(season_id, 1) == {1, 3}


def test_settings(db):
    assert db.get_setting("k") is None
    db.set_setting("k", "v1")
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"


def test_list_participants_registration_order(db):
    """list_participants must return registration order (the sheet layout).

    Regression (2026-10-04): it used to sort by discord_id, so the sheet
    row mapping re-sorted participants on every registration and mislabeled
    pre-created rows. Ties (same registration second) break by discord_id,
    keeping the order deterministic.
    """
    def register_at(discord_id, name, at):
        db.add_participant(discord_id, name)
        db.execute(
            "UPDATE participants SET registered_at_utc = ? WHERE discord_id = ?",
            (at, discord_id),
        )

    register_at(300, "first", "2026-10-04T19:57:00+00:00")
    register_at(100, "second", "2026-10-04T19:57:30+00:00")
    register_at(200, "third", "2026-10-04T19:58:00+00:00")
    names = [r["display_name"] for r in db.list_participants()]
    assert names == ["first", "second", "third"]  # not discord-id order
    # Later registrations never shift earlier participants.
    register_at(50, "fourth", "2026-10-04T19:59:00+00:00")
    assert [r["display_name"] for r in db.list_participants()][:3] == names


def test_dispatched_event_markers(db):
    assert not db.is_event_dispatched(1, "period_start", 1)
    assert not db.is_event_dispatched(1, "season_end", None)
    db.mark_event_dispatched(1, "period_start", 1)
    db.mark_event_dispatched(1, "season_end", None)
    db.mark_event_dispatched(2, "period_start", 1)
    assert db.is_event_dispatched(1, "period_start", 1)
    assert db.is_event_dispatched(1, "season_end", None)
    assert db.is_event_dispatched(2, "period_start", 1)
    # Different round / kind / season are independent.
    assert not db.is_event_dispatched(1, "period_start", 2)
    assert not db.is_event_dispatched(1, "period_reminder", 1)
    assert not db.is_event_dispatched(3, "period_start", 1)
    # Marking twice is harmless.
    db.mark_event_dispatched(1, "period_start", 1)
    assert db.is_event_dispatched(1, "period_start", 1)
