"""Small shared helpers."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("league")


def iso_utc(dt: datetime) -> str:
    """Format an aware datetime as a UTC ISO 8601 string (seconds precision)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_http_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")
