"""Tests for the exact-timer scheduler: event math, boot catch-up and timers."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db import Database
from periods import SeasonSpec
from scheduler import ScheduledEvent, Scheduler, season_events

UTC = timezone.utc
START = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
WEEK = timedelta(days=7)


def make_spec(start=START, length=WEEK, periods=3, season_id=1) -> SeasonSpec:
    return SeasonSpec(season_id=season_id, start_at=start,
                      period_length=length, num_periods=periods)


class TestSeasonEvents:
    def test_kinds_and_order_for_three_weekly_rounds(self):
        events = season_events(make_spec())
        # (kind, period, offset from season start)
        expected = [
            ("period_start", 1, timedelta(0)),
            ("period_reminder", 1, WEEK - timedelta(hours=24)),
            ("period_start", 2, WEEK),
            ("period_reminder", 2, 2 * WEEK - timedelta(hours=24)),
            ("period_start", 3, 2 * WEEK),
            ("period_reminder", 3, 3 * WEEK - timedelta(hours=24)),
            ("season_end", None, 3 * WEEK),
        ]
        actual = [(e.kind, e.period_index, e.at - START) for e in events]
        assert actual == expected

    def test_no_round_end_or_season_start_events(self):
        """Round ends are not announced (only the season end is), and there
        is no separate season-start event (round 1's start is it) — so no
        two events ever share an instant and the fire order is deterministic."""
        events = season_events(make_spec())
        assert all(e.kind not in ("period_end", "season_start") for e in events)
        instants = [e.at for e in events]
        assert len(set(instants)) == len(instants)  # all distinct

    def test_season_end_is_the_final_event(self):
        spec = make_spec()
        events = season_events(spec)
        last_start = next(
            e for e in events if e.kind == "period_start" and e.period_index == 3
        )
        assert events[-1].kind == "season_end"
        assert events[-1].at == spec.end_at
        assert events[-1].at > last_start.at

    def test_no_reminder_for_rounds_shorter_than_24h(self):
        events = season_events(make_spec(length=timedelta(hours=12), periods=2))
        assert all(e.kind != "period_reminder" for e in events)

    def test_reminder_lands_inside_longer_rounds(self):
        spec = make_spec(length=timedelta(days=2), periods=2)
        events = season_events(spec)
        reminders = [e for e in events if e.kind == "period_reminder"]
        assert len(reminders) == 2
        for reminder in reminders:
            assert reminder.at > spec.period_start(reminder.period_index)
            assert reminder.at < spec.period_end(reminder.period_index)

    def test_reminder_is_exactly_24h_before_round_end(self):
        spec = make_spec()
        events = season_events(spec)
        reminder = next(e for e in events if e.kind == "period_reminder" and e.period_index == 2)
        assert spec.period_end(2) - reminder.at == timedelta(hours=24)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


class TestScheduler:
    def test_catchup_runs_missed_events_in_order(self, db):
        """Events that should have happened while the bot was offline are
        replayed in chronological order; future events stay as timers."""
        now = datetime.now(UTC)
        start = (now - timedelta(days=15)).isoformat()  # 15d into a 21d season
        season_id = db.create_season(7 * 86400, 3, start)

        dispatched: list[tuple[str, int | None, int]] = []
        captured: dict[str, object] = {}

        async def dispatch(event):
            dispatched.append((event.kind, event.period_index, event.season_id))

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            await asyncio.wait_for(sched._queue.join(), timeout=5)
            captured["pending"] = set(sched.pending())
            sched.stop()

        asyncio.run(run())

        assert dispatched == [
            ("period_start", 1, season_id),
            ("period_reminder", 1, season_id),
            ("period_start", 2, season_id),
            ("period_reminder", 2, season_id),
            ("period_start", 3, season_id),
        ]
        # The remaining future events are armed as timers.
        assert captured["pending"] == {  # type: ignore[comparison-overlap]
            ("period_reminder", 3), ("season_end", None),
        }

    def test_future_events_armed_as_timers(self, db):
        now = datetime.now(UTC)
        db.create_season(7 * 86400, 3, (now + timedelta(hours=1)).isoformat())

        dispatched: list[ScheduledEvent] = []
        captured: dict[str, object] = {}

        async def dispatch(event):
            dispatched.append(event)

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            await asyncio.sleep(0)
            captured["pending"] = set(sched.pending())
            sched.stop()

        asyncio.run(run())

        assert dispatched == []  # nothing fired yet
        # All 7 events of the season are in the future.
        assert captured["pending"] == {  # type: ignore[comparison-overlap]
            ("period_start", 1), ("period_reminder", 1),
            ("period_start", 2), ("period_reminder", 2),
            ("period_start", 3), ("period_reminder", 3),
            ("season_end", None),
        }

    def test_timer_fires_when_due(self, db):
        now = datetime.now(UTC)
        db.create_season(86400, 1, (now + timedelta(seconds=0.3)).isoformat())

        dispatched: list[tuple[str, int | None]] = []

        async def dispatch(event):
            dispatched.append((event.kind, event.period_index))

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            await asyncio.sleep(0.6)  # the timers are 0.3s out
            sched.stop()

        asyncio.run(run())

        # 24h rounds get no reminder; only round 1's start fires now (the
        # season end is still 24h out).
        assert sorted(dispatched) == [("period_start", 1)]

    def test_reschedule_replaces_timers(self, db):
        now = datetime.now(UTC)
        db.create_season(7 * 86400, 3, (now + timedelta(hours=1)).isoformat())
        captured: dict[str, object] = {}

        async def dispatch(event):
            pass

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            captured["before"] = len(sched.pending())
            # A new season replaces the old one: timers are recomputed.
            db.create_season(86400, 1, (now + timedelta(hours=2)).isoformat())
            sched.reschedule()
            captured["after"] = set(sched.pending())
            sched.stop()

        asyncio.run(run())

        assert captured["before"] == 7
        # A single 24h round: no reminder; both its events are still future.
        assert captured["after"] == {  # type: ignore[comparison-overlap]
            ("period_start", 1), ("season_end", None),
        }

    def test_stale_catchup_dropped_after_reschedule(self, db):
        """Events queued for a season that got superseded are never dispatched."""
        now = datetime.now(UTC)
        # Season A ended 9 days ago: all its events would be catch-up.
        db.create_season(7 * 86400, 3, (now - timedelta(days=30)).isoformat())

        dispatched: list[str] = []

        async def dispatch(event):
            dispatched.append(event.kind)

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            # Replace the season before the worker gets to run.
            db.create_season(7 * 86400, 2, (now + timedelta(hours=1)).isoformat())
            sched.reschedule()
            await asyncio.wait_for(sched._queue.join(), timeout=5)
            sched.stop()

        asyncio.run(run())

        assert dispatched == []

    def test_no_season_is_a_noop(self, db):
        dispatched: list[ScheduledEvent] = []

        async def dispatch(event):
            dispatched.append(event)

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            await asyncio.sleep(0)
            assert sched.pending() == []
            sched.stop()

        asyncio.run(run())

        assert dispatched == []

    def test_reschedule_from_worker_thread(self, db):
        """Regression: slash commands run via asyncio.to_thread on a worker
        thread; reschedule must move the timer arming onto the event loop
        instead of raising 'no running event loop'."""
        now = datetime.now(UTC)
        db.create_season(86400, 1, (now + timedelta(hours=2)).isoformat())
        captured: dict[str, object] = {}

        async def dispatch(event):
            pass

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            # Simulate the command's worker thread.
            await asyncio.to_thread(sched.reschedule)
            await asyncio.sleep(0.05)  # let the queued reschedule run
            captured["pending"] = set(sched.pending())
            sched.stop()

        asyncio.run(run())

        # A single 24h round starting in 2h: no reminder, all events future.
        assert captured["pending"] == {  # type: ignore[comparison-overlap]
            ("period_start", 1), ("season_end", None),
        }

    def test_stop_cancels_queued_worker_reschedule(self, db):
        """A reschedule queued from a worker thread must not re-arm timers
        after the scheduler has been stopped."""
        now = datetime.now(UTC)
        db.create_season(86400, 1, (now + timedelta(hours=2)).isoformat())

        async def dispatch(event):
            pass

        async def run():
            sched = Scheduler(db)
            sched.start(dispatch)
            await asyncio.to_thread(sched.reschedule)
            sched.stop()  # before the queued reschedule gets to run
            await asyncio.sleep(0.05)
            return sched.pending()

        assert asyncio.run(run()) == []
