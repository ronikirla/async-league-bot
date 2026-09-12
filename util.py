"""Small shared helpers."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("league")


def format_duration(td: timedelta) -> str:
    """Format a duration without microsecond noise, e.g. ``1d 2h 5m`` or ``12s``."""
    total = int(round(td.total_seconds()))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def iso_utc(dt: datetime) -> str:
    """Format an aware datetime as a UTC ISO 8601 string (seconds precision)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def discord_timestamp(dt: datetime, style: str = "f") -> str:
    """Format an aware datetime as a Discord localized timestamp, e.g. ``<t:123:f>``.

    Discord renders the timestamp in the *viewer's* configured time zone.
    Common styles: ``d`` (day), ``t`` (time), ``f`` (full date + time),
    ``F`` (long date + time), ``R`` (relative, "2 hours ago").
    """
    return f"<t:{int(dt.timestamp())}:{style}>"


def is_http_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")
