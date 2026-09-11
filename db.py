"""SQLite persistence layer.

Single-file database (``league.db`` by default). One connection per thread
is enough here because the bot is a single process and sqlite is used
synchronously from the asyncio event loop for short transactions.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_length_seconds INTEGER NOT NULL,
    num_periods INTEGER NOT NULL,
    start_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    discord_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    registered_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS period_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER NOT NULL,
    period_index INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    seed TEXT,
    seed_requested_at_utc TEXT,
    submitted_at_utc TEXT,
    run_time TEXT,
    video_url TEXT,
    UNIQUE (season_id, period_index, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_records_lookup
    ON period_records (season_id, period_index, discord_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SettingKey = str
PARTICIPANT_ROLE_KEY = "participant_role_id"
SEED_NOT_DONE_ROLE_KEY = "seed_not_done_role_id"
SEED_CHANNEL_KEY = "seed_channel_id"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str = "league.db"):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- low level ------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- settings -------------------------------------------------------
    def get_setting(self, key: SettingKey) -> Optional[str]:
        rows = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_setting(self, key: SettingKey, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- seasons --------------------------------------------------------
    def create_season(
        self, period_length_seconds: int, num_periods: int, start_at_utc: str
    ) -> int:
        cur = self.execute(
            "INSERT INTO seasons (period_length_seconds, num_periods, start_at_utc, created_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (period_length_seconds, num_periods, start_at_utc, utcnow_iso()),
        )
        return int(cur.lastrowid)

    def get_latest_season(self) -> Optional[sqlite3.Row]:
        rows = self.query("SELECT * FROM seasons ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def get_season(self, season_id: int) -> Optional[sqlite3.Row]:
        rows = self.query("SELECT * FROM seasons WHERE id = ?", (season_id,))
        return rows[0] if rows else None

    # -- participants ---------------------------------------------------
    def add_participant(self, discord_id: int, display_name: str) -> bool:
        """Insert a participant. Returns False if already registered."""
        try:
            self.execute(
                "INSERT INTO participants (discord_id, display_name, registered_at_utc) "
                "VALUES (?, ?, ?)",
                (discord_id, display_name, utcnow_iso()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_participant(self, discord_id: int) -> None:
        self.execute("DELETE FROM participants WHERE discord_id = ?", (discord_id,))

    def is_participant(self, discord_id: int) -> bool:
        rows = self.query("SELECT 1 FROM participants WHERE discord_id = ?", (discord_id,))
        return bool(rows)

    def get_participant(self, discord_id: int) -> Optional[sqlite3.Row]:
        rows = self.query("SELECT * FROM participants WHERE discord_id = ?", (discord_id,))
        return rows[0] if rows else None

    def list_participants(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM participants ORDER BY discord_id")

    # -- period records ---------------------------------------------------
    def record_exists(
        self, season_id: int, period_index: int, discord_id: int
    ) -> Optional[sqlite3.Row]:
        rows = self.query(
            "SELECT * FROM period_records WHERE season_id = ? AND period_index = ? AND discord_id = ?",
            (season_id, period_index, discord_id),
        )
        return rows[0] if rows else None

    def create_record(self, season_id: int, period_index: int, discord_id: int) -> bool:
        """Insert an empty record. Returns False if it already exists."""
        try:
            self.execute(
                "INSERT INTO period_records (season_id, period_index, discord_id) "
                "VALUES (?, ?, ?)",
                (season_id, period_index, discord_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_record_seed(self, season_id: int, period_index: int, seed: str) -> None:
        """Set the period seed (all participants share it). Idempotent."""
        self.execute(
            "UPDATE period_records SET seed = ? "
            "WHERE season_id = ? AND period_index = ? AND seed IS NULL",
            (seed, season_id, period_index),
        )

    def get_period_seed(self, season_id: int, period_index: int) -> Optional[str]:
        rows = self.query(
            "SELECT seed FROM period_records WHERE season_id = ? AND period_index = ? "
            "LIMIT 1",
            (season_id, period_index),
        )
        if rows and rows[0]["seed"] is not None:
            return rows[0]["seed"]
        return None

    def mark_seed_requested(
        self, season_id: int, period_index: int, discord_id: int
    ) -> bool:
        """Record the FIRST seed request timestamp.

        Returns True if this call stored the timestamp, False if the
        participant had already requested the seed earlier.
        """
        cur = self.execute(
            "UPDATE period_records SET seed_requested_at_utc = ? "
            "WHERE season_id = ? AND period_index = ? AND discord_id = ? "
            "AND seed_requested_at_utc IS NULL",
            (utcnow_iso(), season_id, period_index, discord_id),
        )
        return cur.rowcount == 1

    def mark_submitted(
        self,
        season_id: int,
        period_index: int,
        discord_id: int,
        run_time: str,
        video_url: str,
    ) -> bool:
        """Store the submission. Returns False if already submitted."""
        cur = self.execute(
            "UPDATE period_records SET submitted_at_utc = ?, run_time = ?, video_url = ? "
            "WHERE season_id = ? AND period_index = ? AND discord_id = ? "
            "AND submitted_at_utc IS NULL",
            (utcnow_iso(), run_time, video_url, season_id, period_index, discord_id),
        )
        return cur.rowcount == 1

    def has_submitted(self, season_id: int, period_index: int, discord_id: int) -> bool:
        rows = self.query(
            "SELECT 1 FROM period_records "
            "WHERE season_id = ? AND period_index = ? AND discord_id = ? "
            "AND submitted_at_utc IS NOT NULL",
            (season_id, period_index, discord_id),
        )
        return bool(rows)

    def unsubmitted_ids(self, season_id: int, period_index: int) -> set[int]:
        """Registered participants who have NOT submitted this period yet."""
        participant_rows = self.query("SELECT discord_id FROM participants")
        submitted = self.submitted_ids(season_id, period_index)
        return {int(r["discord_id"]) for r in participant_rows} - submitted

    def submitted_ids(self, season_id: int, period_index: int) -> set[int]:
        rows = self.query(
            "SELECT discord_id FROM period_records "
            "WHERE season_id = ? AND period_index = ? AND submitted_at_utc IS NOT NULL",
            (season_id, period_index),
        )
        return {int(r["discord_id"]) for r in rows}

    def participant_sheet_row(self, discord_id: int) -> Optional[int]:
        """Deterministic 1-based spreadsheet row for a participant.

        Rows are ordered by registration order (ties broken by discord_id),
        with row 1 reserved for the header. Later registrants are always
        appended after existing rows, so the mapping is stable.
        """
        row = self.get_participant(discord_id)
        if not row:
            return None
        rows = self.query(
            "SELECT COUNT(*) AS c FROM participants "
            "WHERE (registered_at_utc, discord_id) < (?, ?)",
            (row["registered_at_utc"], discord_id),
        )
        return int(rows[0]["c"]) + 2
