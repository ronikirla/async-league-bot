"""Unit tests for period math and time parsing."""
from datetime import datetime, timedelta, timezone

import pytest

from periods import (
    PeriodError,
    SeasonSpec,
    current_season_state,
    format_run_time,
    parse_period_length,
    parse_run_time,
    parse_start_time,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def make_spec(start=NOW - timedelta(days=8), length=timedelta(days=7), periods=4):
    return SeasonSpec(season_id=1, start_at=start, period_length=length, num_periods=periods)


class TestPeriodMath:
    def test_index_within_first_period(self):
        spec = make_spec()
        assert spec.period_index_at(NOW - timedelta(days=6)) == 1

    def test_index_within_second_period(self):
        spec = make_spec()
        assert spec.period_index_at(NOW - timedelta(hours=12)) == 2

    def test_index_before_start_is_none(self):
        spec = make_spec()
        assert spec.period_index_at(NOW - timedelta(days=10)) is None

    def test_index_after_end_is_none(self):
        spec = make_spec()
        assert spec.period_index_at(spec.end_at) is None
        assert spec.period_index_at(spec.end_at + timedelta(seconds=1)) is None

    def test_last_period_boundary(self):
        spec = make_spec()
        assert spec.period_index_at(spec.end_at - timedelta(seconds=1)) == spec.num_periods

    def test_end_at(self):
        spec = make_spec()
        assert spec.end_at == spec.start_at + timedelta(weeks=4)

    def test_period_window(self):
        spec = make_spec()
        start, end = spec.current_period_window(NOW - timedelta(hours=12))
        assert start == spec.start_at + timedelta(weeks=1)
        assert end == spec.start_at + timedelta(weeks=2)


class TestPeriodLength:
    def test_days(self):
        assert parse_period_length("7d") == timedelta(days=7)

    def test_hours(self):
        assert parse_period_length("12h") == timedelta(hours=12)

    def test_minutes(self):
        assert parse_period_length("90m") == timedelta(minutes=90)

    def test_combined(self):
        assert parse_period_length("1d12h") == timedelta(days=1, hours=12)

    def test_whitespace_and_case(self):
        assert parse_period_length(" 7D ") == timedelta(days=7)

    def test_invalid(self):
        for bad in ("", "abc", "7x", "7 days", "-1d", "d7"):
            with pytest.raises(PeriodError):
                parse_period_length(bad)


class TestRunTime:
    def test_minutes_seconds(self):
        assert parse_run_time("12:34") == 754.0

    def test_sub_minute(self):
        assert parse_run_time("0:01") == 1.0
        assert parse_run_time("0:45.5") == pytest.approx(45.5)

    def test_milliseconds(self):
        assert parse_run_time("12:34.567") == pytest.approx(754.567)

    def test_hours(self):
        assert parse_run_time("1:02:03.45") == pytest.approx(3723.45)

    def test_invalid(self):
        for bad in ("", "abc", "1:", ":34", "1:60", "60", "1:02:03.1234", "-1:00"):
            with pytest.raises(PeriodError):
                parse_run_time(bad)

    def test_format_roundtrip(self):
        assert format_run_time(754.567) == "12:34.567"
        assert format_run_time(3723.45) == "1:02:03.450"
        assert format_run_time(59.0) == "0:59.000"

    def test_format_carries(self):
        assert format_run_time(59.9996) == "1:00.000"


class TestStartTime:
    def test_now_rounds_up(self):
        result = parse_start_time("now", NOW)
        assert result == datetime(2026, 9, 10, 12, 1, 0, tzinfo=UTC)

    def test_iso_naive_is_utc(self):
        result = parse_start_time("2026-09-14T12:00:00", NOW)
        assert result == datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    def test_iso_with_offset_converts(self):
        result = parse_start_time("2026-09-14T15:00:00+03:00", NOW)
        assert result == datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    def test_invalid(self):
        with pytest.raises(PeriodError):
            parse_start_time("next monday", NOW)


class TestCurrentSeasonState:
    def test_no_season(self, tmp_path):
        from db import Database

        db = Database(str(tmp_path / "test.db"))
        assert current_season_state(db, NOW) is None
        db.close()

    def test_active_period(self, tmp_path):
        from db import Database

        db = Database(str(tmp_path / "test.db"))
        start = (NOW - timedelta(days=8)).isoformat()
        db.create_season(7 * 86400, 4, start)
        state = current_season_state(db, NOW)
        assert state is not None
        assert state.in_season is True
        assert state.period_index == 2
        assert state.period_start == NOW - timedelta(days=1)
        assert state.period_end == NOW + timedelta(days=6)
        db.close()

    def test_ended_season(self, tmp_path):
        from db import Database

        db = Database(str(tmp_path / "test.db"))
        start = (NOW - timedelta(days=30)).isoformat()
        db.create_season(7 * 86400, 4, start)
        state = current_season_state(db, NOW)
        assert state is not None
        assert state.in_season is False
        assert state.period_index is None
        db.close()
