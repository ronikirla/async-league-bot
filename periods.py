"""Season/period math and user-facing time parsing.

A season is fully described by:
- ``start_at``: UTC datetime when the first period begins
- ``period_length``: length of one period (e.g. one week)
- ``num_periods``: how many periods make up the season

The active period index is derived from the current UTC clock, so the bot
stays correct across restarts and time zones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class PeriodError(Exception):
    """Raised for invalid period-length or run-time inputs."""


@dataclass(frozen=True)
class SeasonSpec:
    season_id: int
    start_at: datetime  # aware, UTC
    period_length: timedelta
    num_periods: int

    @property
    def end_at(self) -> datetime:
        return self.start_at + self.period_length * self.num_periods

    def period_start(self, index: int) -> datetime:
        return self.start_at + self.period_length * (index - 1)

    def period_end(self, index: int) -> datetime:
        return self.start_at + self.period_length * index

    def period_index_at(self, now: datetime) -> int | None:
        """1-based period index containing ``now``, or None if out of range."""
        if now < self.start_at or now >= self.end_at:
            return None
        delta = now - self.start_at
        return delta // self.period_length + 1

    def next_period_start(self, now: datetime) -> datetime | None:
        """When the NEXT period starts, or None if the season is over."""
        idx = self.period_index_at(now)
        if idx is None or idx >= self.num_periods:
            return None
        return self.period_end(idx)

    def current_period_window(self, now: datetime) -> tuple[datetime, datetime] | None:
        idx = self.period_index_at(now)
        if idx is None:
            return None
        return self.period_start(idx), self.period_end(idx)


# --- active season state -------------------------------------------------


@dataclass(frozen=True)
class SeasonState:
    """Snapshot of the active season at a point in time."""
    season: SeasonSpec
    in_season: bool          # is the season currently in progress (not ended)?
    period_index: int | None  # 1-based active period, None if before start or ended
    period_start: datetime | None
    period_end: datetime | None


def current_season_state(db, now: datetime | None = None) -> SeasonState | None:
    """Derive the active season state from the database and the clock.

    Returns ``None`` if no season has been created yet.
    """
    now = now or datetime.now(timezone.utc)
    row = db.get_latest_season()
    if row is None:
        return None
    spec = SeasonSpec(
        season_id=int(row["id"]),
        start_at=datetime.fromisoformat(row["start_at_utc"]),
        period_length=timedelta(seconds=int(row["period_length_seconds"])),
        num_periods=int(row["num_periods"]),
    )
    if now < spec.start_at:
        return SeasonState(season=spec, in_season=False, period_index=None,
                           period_start=None, period_end=None)
    if now >= spec.end_at:
        return SeasonState(season=spec, in_season=False, period_index=None,
                           period_start=None, period_end=None)
    idx = spec.period_index_at(now)
    return SeasonState(
        season=spec,
        in_season=True,
        period_index=idx,
        period_start=spec.period_start(idx),
        period_end=spec.period_end(idx),
    )


# --- period length parsing ---------------------------------------------

# e.g. "7d", "14d", "12h", "36h", "1d12h", "90m"
_PERIOD_FULL_RE = re.compile(r"(?:\d+\s*[dhm]\s?)+", re.IGNORECASE)
_PERIOD_TERM_RE = re.compile(r"(\d+)\s*([dhm])", re.IGNORECASE)


def parse_period_length(text: str) -> timedelta:
    """Parse a human-friendly period length like ``7d`` or ``1d12h``."""
    text = text.strip().lower()
    if not text:
        raise PeriodError("Period length is empty")
    if not _PERIOD_FULL_RE.fullmatch(text):
        raise PeriodError(
            "Invalid period length. Use forms like 7d, 12h, 30m or 1d12h "
            "(d=days, h=hours, m=minutes)."
        )
    days, hours, minutes = 0, 0, 0
    for num, unit in _PERIOD_TERM_RE.findall(text):
        n = int(num)
        if unit == "d":
            days += n
        elif unit == "h":
            hours += n
        else:
            minutes += n
    if days + hours + minutes == 0:
        raise PeriodError("Period length must be greater than zero")
    return timedelta(days=days, hours=hours, minutes=minutes)


# --- run time parsing ----------------------------------------------------

# Accepts "M:SS", "H:MM:SS" and optional fractional milliseconds ".mmm"
_RUN_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?$")


def parse_run_time(text: str) -> float:
    """Parse a run time into total seconds (float).

    Accepts ``M:SS``, ``H:MM:SS`` and optional ``.mmm`` milliseconds,
    e.g. ``12:34.567`` -> ``754.567``.
    """
    text = text.strip()
    m = _RUN_TIME_RE.match(text)
    if not m:
        raise PeriodError(
            "Invalid run time. Use M:SS or H:MM:SS with optional .mmm "
            "milliseconds, e.g. 12:34.567"
        )
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    frac = m.group(4)
    if minutes > 59 or seconds > 59:
        raise PeriodError("Invalid run time: minutes and seconds must be under 60")
    if hours == 0 and minutes == 0:
        raise PeriodError("Invalid run time: time must be greater than zero")
    return hours * 3600 + minutes * 60 + seconds + (int(frac.ljust(3, "0")) / 1000.0 if frac else 0.0)


def format_run_time(seconds: float) -> str:
    """Format total seconds as ``M:SS.mmm`` (or ``H:MM:SS.mmm``)."""
    ms = round((seconds - int(seconds)) * 1000)
    if ms == 1000:
        seconds = int(seconds) + 1
        ms = 0
    total_seconds = int(seconds)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{base}.{ms:03d}"


# --- start time parsing ----------------------------------------------------

def parse_start_time(text: str, now: datetime) -> datetime:
    """Parse the season start time given by an admin.

    Accepts:
    - ``now`` / empty -> the current time (rounded up to the next minute)
    - ISO 8601 datetimes, with or without timezone info
      (naive values are assumed UTC)
    """
    text = (text or "").strip()
    if not text or text.lower() == "now":
        return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PeriodError(
            "Invalid start time. Use ISO 8601 (e.g. 2026-09-14T12:00:00) or 'now'."
        ) from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
