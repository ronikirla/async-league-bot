"""Exact-timer scheduler for season/round events.

Replaces the old periodic reconciliation loop. Every event the bot must act
on has a known wall-clock time derived from the season spec:

- for each round: start, and a 24h-before-end reminder
- season end

Round ends are deliberately NOT scheduled: rounds are contiguous (round i
ends exactly when round i+1 starts), so a round-end event would share its
instant with the next round's start — and same-instant timers have no
guaranteed fire order. The round-start routine fully re-syncs the roles at
every boundary, and the season-end event does the final strip, so nothing
is lost by not having a round-end timer.

The scheduler arms one ``asyncio`` timer per pending event. At boot it
replays every event whose time has already passed (catch-up), so restarts
and downtime are safe without persisting any scheduler state — the timers
are re-derived from the database on every boot.

Events that do not need a timer of their own are handled inline by the
routines that already run at the right moment (e.g. the seed-not-done role
is granted at round start and removed on submit/DNF and at round end —
never by a background poll).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from db import Database
from periods import SeasonSpec
from util import now_utc

log = logging.getLogger("league")

# How long before a round ends the "seed not done" reminder fires.
REMINDER_BEFORE_END = timedelta(hours=24)

# Ordering of events that share the same instant: the season-start
# announcement comes before round 1's start, round N's end announcement
# comes before round N+1's start, and the last round's end comes before the
# season-end announcement.
_SEASON_RANK = {
    "season_start": 0,
    "period_start": 1,
    "period_reminder": 1,
    "period_end": 1,
    "season_end": 2,
}


@dataclass(frozen=True)
class ScheduledEvent:
    """One point in time at which the bot must do something."""

    kind: str          # one of _SEASON_RANK
    at: datetime       # aware UTC
    period_index: Optional[int]  # 1-based round index, None for season-level events
    season_id: int = 0  # season the event belongs to (guards against stale events)

    def sort_key(self) -> tuple[datetime, int, int]:
        return (self.at, _SEASON_RANK[self.kind], self.period_index or 0)


def season_events(spec: SeasonSpec) -> list[ScheduledEvent]:
    """All event times for a season, in chronological order.

    Round 1's start doubles as the season start — they share the same
    instant, which is exactly why there is no separate season-start event.
    """
    events: list[ScheduledEvent] = []
    for index in range(1, spec.num_periods + 1):
        events.append(ScheduledEvent("period_start", spec.period_start(index), index))
        reminder_at = spec.period_end(index) - REMINDER_BEFORE_END
        # Only schedule the reminder if it lands inside the round (rounds of
        # 24h or less get no reminder).
        if reminder_at > spec.period_start(index):
            events.append(ScheduledEvent("period_reminder", reminder_at, index))
    events.append(ScheduledEvent("season_end", spec.end_at, None))
    events.sort(key=lambda e: e.sort_key())
    return events


class Scheduler:
    """Arms exact timers for season events and replays missed ones in order.

    Fired events are funneled through a single worker task so catch-up
    events (and events whose timers expire while another dispatch is still
    running) are handled strictly one at a time, in chronological order.
    """

    def __init__(self, db: Database):
        self._db = db
        self._dispatch: Optional[Callable[[ScheduledEvent], Awaitable[None]]] = None
        self._handles: dict[tuple[str, Optional[int]], asyncio.TimerHandle] = {}
        self._queue: "asyncio.Queue[tuple[int, ScheduledEvent]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._generation = 0
        self._started = False

    # -- public API -------------------------------------------------------
    def start(self, dispatch: Callable[[ScheduledEvent], Awaitable[None]]) -> None:
        """Start the worker, restore timers and run any missed events.

        ``dispatch`` is awaited for every event (past and future alike).
        Must be called once the bot is ready; repeated calls are no-ops.
        """
        if self._started:
            return
        self._started = True
        # A fresh queue per start: asyncio.Queue binds itself to the loop
        # that first uses it, and a restart may run on a new loop (each
        # asyncio.run creates one; production uses a single long-lived one).
        self._queue: "asyncio.Queue[tuple[int, ScheduledEvent]]" = asyncio.Queue()
        # Remember the bot's loop: asyncio timers can only be created on the
        # loop that runs them, and reschedule() may be called from a worker
        # thread (commands run via asyncio.to_thread).
        self._loop = asyncio.get_running_loop()
        self._dispatch = dispatch
        self._worker = asyncio.create_task(self._run_worker())
        self.reschedule()

    def stop(self) -> None:
        self._started = False
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    def reschedule(self) -> None:
        """Recompute all timers from the database (e.g. after /create_season).

        Thread-safe: the actual re-derivation runs on the bot's event loop,
        so calling this from a worker thread (slash commands run via
        ``asyncio.to_thread``) simply hands the work over.
        """
        if not self._started:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._do_reschedule()
        else:
            try:
                loop.call_soon_threadsafe(self._do_reschedule)
            except RuntimeError:
                log.warning("Scheduler: event loop is closed; timers not rescheduled")

    def _do_reschedule(self) -> None:
        """Cancel every pending timer and re-arm from the database.

        Events whose time has already passed are queued for immediate
        catch-up; the rest are armed as timers. Anything already queued for
        an older generation is dropped by the worker, so replacing a season
        can never double-fire stale events.
        """
        if not self._started:
            return  # stopped while this was queued from a worker thread
        self._generation += 1
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()

        row = self._db.get_latest_season()
        if row is None:
            log.info("Scheduler: no season exists yet, nothing to schedule")
            return
        spec = SeasonSpec(
            season_id=int(row["id"]),
            start_at=datetime.fromisoformat(row["start_at_utc"]),
            period_length=timedelta(seconds=int(row["period_length_seconds"])),
            num_periods=int(row["num_periods"]),
        )
        # One clock snapshot for the whole pass: computing every timer's
        # delay from the same ``now`` keeps distinct instants strictly
        # ordered instead of letting clock drift reorder same-instant
        # timers randomly.
        now = now_utc()
        season_id = spec.season_id
        catch_up = 0
        for event in season_events(spec):
            event = replace(event, season_id=season_id)
            if event.at <= now:
                # Catch-up: run anything that should have already happened
                # (covers downtime across restarts).
                self._enqueue(self._generation, event)
                catch_up += 1
            else:
                self._arm(event, now)
        log.info(
            "Scheduler: season %d — %d catch-up event(s) queued, %d timer(s) armed",
            season_id, catch_up, len(self._handles),
        )

    # -- internals --------------------------------------------------------
    def _arm(self, event: ScheduledEvent, now: datetime) -> None:
        delay = max(0.0, (event.at - now).total_seconds())
        key = (event.kind, event.period_index)
        loop = self._loop if self._loop is not None else asyncio.get_running_loop()
        self._handles[key] = loop.call_later(
            delay, self._enqueue, self._generation, event
        )

    def _enqueue(self, generation: int, event: ScheduledEvent) -> None:
        self._queue.put_nowait((generation, event))

    async def _run_worker(self) -> None:
        while True:
            generation, event = await self._queue.get()
            try:
                if generation != self._generation:
                    continue  # superseded by a newer season / reschedule
                await self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Scheduler: dispatch failed for %s (season %s, round %s)",
                    event.kind, event.season_id, event.period_index,
                )
            finally:
                self._queue.task_done()

    # -- introspection (tests) ----------------------------------------------
    def pending(self) -> list[tuple[str, Optional[int]]]:
        """Keys of the currently armed timers."""
        return sorted(self._handles, key=lambda k: (k[0], k[1] if k[1] is not None else -1))


__all__ = [
    "REMINDER_BEFORE_END",
    "ScheduledEvent",
    "Scheduler",
    "season_events",
]
