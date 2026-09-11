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
    assert db.mark_submitted(season_id, 1, 1, "10:00.000", "https://youtu.be/x") is True
    assert db.has_submitted(season_id, 1, 1)
    assert db.mark_submitted(season_id, 1, 1, "9:59.000", "https://youtu.be/y") is False
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
    db.mark_submitted(season_id, 1, 2, "10:00.000", "https://youtu.be/x")
    assert db.unsubmitted_ids(season_id, 1) == {1, 3}


def test_settings(db):
    assert db.get_setting("k") is None
    db.set_setting("k", "v1")
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"


def test_sheet_row_is_stable(db):
    db.add_participant(1, "first")
    row_first = db.participant_sheet_row(1)
    db.add_participant(2, "second")
    assert db.participant_sheet_row(1) == row_first  # later registrants don't shift rows
    assert db.participant_sheet_row(2) == row_first + 1
    assert row_first == 2  # row 1 is the header
