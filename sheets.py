"""Google Sheets synchronization.

One worksheet (tab) per season in the configured spreadsheet, titled
``S<season_id>``. Layout: row 1 is the header, then one block of rows per
participant (in registration order); each block holds one row per period.
Participant-major ordering keeps the row mapping stable when new
participants register mid-season (their rows are appended at the bottom,
existing rows never shift).

Columns:

    Period | Period Start (UTC) | Period End (UTC) | Participant |
    Discord ID | Seed | Seed Requested At (UTC) | Submitted At (UTC) |
    Run Time | Video

Dynamic cells (seed, timestamps, run time, video) are written as
single-cell updates so concurrent requests never clobber each other.

With ``DRY_RUN=1`` no network calls are made; every write is logged and
appended to ``dry_run_rows.json`` so behavior can be verified locally.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from config import Config
from db import Database
from periods import SeasonSpec
from util import iso_utc

try:
    import gspread
    from gspread.exceptions import APIError, WorksheetNotFound
except ImportError:  # gspread is optional in dry-run mode
    gspread = None
    APIError = Exception
    WorksheetNotFound = Exception

log = logging.getLogger("league")


class SheetsError(Exception):
    """Raised when Google Sheets cannot be reached or is misconfigured."""


# Column layout (1-based).
COL_PERIOD = 1
COL_PERIOD_START = 2
COL_PERIOD_END = 3
COL_PARTICIPANT = 4
COL_DISCORD_ID = 5
COL_SEED = 6
COL_SEED_REQUESTED_AT = 7
COL_SUBMITTED_AT = 8
COL_RUN_TIME = 9
COL_VIDEO = 10
NUM_COLUMNS = 10

HEADER = [
    "Period",
    "Period Start (UTC)",
    "Period End (UTC)",
    "Participant",
    "Discord ID",
    "Seed",
    "Seed Requested At (UTC)",
    "Submitted At (UTC)",
    "Run Time",
    "Video",
]

DRY_RUN_LOG_FILE = "dry_run_rows.json"


def _col_letter(idx: int) -> str:
    """1 -> A, 2 -> B, ... 27 -> AA (more than enough for our 10 columns)."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


class SheetService:
    def __init__(self, config: Config, db: Database):
        self._config = config
        self._db = db
        self._gs_client = None
        self._spreadsheet = None

    # -- client management ---------------------------------------------
    @property
    def dry_run(self) -> bool:
        return self._config.dry_run

    def _client(self):
        if self.dry_run:
            return None
        if gspread is None:
            raise SheetsError("gspread is not installed. Run: pip install -r requirements.txt")
        if not os.path.exists(self._config.service_account_file):
            raise SheetsError(
                f"Service account key file not found: {self._config.service_account_file}. "
                "Download the JSON key and place it there (or set DRY_RUN=1 to test without Google)."
            )
        if self._gs_client is None:
            try:
                self._gs_client = gspread.service_account(
                    filename=self._config.service_account_file
                )
            except APIError as exc:
                raise SheetsError(f"Failed to authenticate with Google: {exc}") from exc
        return self._gs_client

    def _sheet(self):
        client = self._client()
        if self.dry_run:
            return None
        if self._spreadsheet is None:
            try:
                self._spreadsheet = client.open_by_key(self._config.spreadsheet_id)
            except APIError as exc:
                raise SheetsError(
                    f"Could not open spreadsheet {self._config.spreadsheet_id!r} "
                    f"(check GOOGLE_SPREADSHEET_ID and that the service account has access). {exc}"
                ) from exc
        return self._spreadsheet

    def verify(self) -> None:
        """Fail fast at startup if the spreadsheet is unreachable (skipped in dry run)."""
        if self.dry_run:
            log.info("DRY_RUN=1: skipping Google Sheets verification")
            return
        self._sheet()
        log.info("Google Sheets connection verified")

    # -- dry-run bookkeeping -------------------------------------------
    def _dry_log(self, operation: str, **fields) -> None:
        log.info("[dry-run] %s %s", operation, json.dumps(fields, default=str))
        try:
            with open(DRY_RUN_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": iso_utc(datetime.now(timezone.utc)), "op": operation, **fields},
                    default=str,
                ) + "\n")
        except OSError:
            pass

    # -- season tabs ------------------------------------------------------
    @staticmethod
    def tab_title(season_id: int) -> str:
        return f"S{season_id}"

    def get_or_create_season_tab(self, season_id: int):
        """Return the worksheet for a season, creating it (with headers) if needed.

        Returns ``None`` in dry-run mode.
        """
        title = self.tab_title(season_id)
        sheet = self._sheet()
        if sheet is None:  # dry run
            self._dry_log("get_or_create_season_tab", season_id=season_id, tab=title)
            return None
        try:
            ws = sheet.worksheet(title)
        except WorksheetNotFound:
            ws = sheet.add_worksheet(title=title, rows=1000, cols=NUM_COLUMNS)
            log.info("Created season tab %r", title)
        # Ensure the header row exists.
        existing_header = ws.row_values(1)
        if existing_header[:NUM_COLUMNS] != HEADER:
            ws.batch_update([{"range": f"A1:{_col_letter(NUM_COLUMNS)}1", "values": [HEADER]}])
            log.info("Wrote header row to season tab %r", title)
        return ws

    # -- row layout -----------------------------------------------------
    def _season_row_map(self, season: SeasonSpec) -> dict[tuple[int, int], int]:
        """Build ``{(period_index, discord_id): sheet_row}`` for a season.

        Rows are grouped by participant (registration order), one row per
        period inside each group. Row 1 is the header. The mapping is
        derived from the same deterministic ordering as
        ``Database.participant_sheet_row`` and is stable across restarts.
        """
        mapping: dict[tuple[int, int], int] = {}
        row = 2
        for p in self._db.list_participants():
            for period_index in range(1, season.num_periods + 1):
                mapping[(period_index, int(p["discord_id"]))] = row
                row += 1
        return mapping

    def ensure_season_rows(self, season: SeasonSpec) -> None:
        """Pre-create the static columns for all (participant x period) rows."""
        ws = self.get_or_create_season_tab(season.season_id)
        if ws is None:  # dry run
            count = len(self._db.list_participants()) * season.num_periods
            self._dry_log("ensure_season_rows", season_id=season.season_id, rows=count)
            return

        mapping = self._season_row_map(season)
        if not mapping:
            log.warning("No registered participants; no rows pre-created for season %s",
                        season.season_id)
            return

        col_a = ws.col_values(1)  # 0-indexed; index i is sheet row i+1
        updates = []
        for (period_index, discord_id), row in mapping.items():
            if row <= len(col_a) and col_a[row - 1] != "":
                continue  # row already exists
            start = season.period_start(period_index)
            end = season.period_end(period_index)
            participant = self._db.get_participant(discord_id)
            name = participant["display_name"] if participant else "?"
            updates.append({
                "range": f"A{row}:{_col_letter(NUM_COLUMNS)}{row}",
                "values": [[
                    period_index,
                    iso_utc(start),
                    iso_utc(end),
                    name,
                    discord_id,
                    "",  # seed
                    "",  # seed requested at
                    "",  # submitted at
                    "",  # run time
                    "",  # video
                ]],
            })
        if updates:
            ws.batch_update(updates)
            log.info("Pre-created %d rows for season %s", len(updates), season.season_id)

    def _target_row(self, season: SeasonSpec, period_index: int, discord_id: int) -> int:
        mapping = self._season_row_map(season)
        row = mapping.get((period_index, discord_id))
        if row is None:
            raise SheetsError(
                f"No sheet row for discord id {discord_id} in season {season.season_id} "
                f"period {period_index}. The participant may have registered after the "
                "season started without re-running the sheet pre-creation."
            )
        return row

    # -- cell writes ------------------------------------------------------
    def write_seed(self, season: SeasonSpec, period_index: int, seed: str) -> None:
        """Write the shared seed into the seed column of every row of the period."""
        self.ensure_season_rows(season)
        ws = self.get_or_create_season_tab(season.season_id)
        if ws is None:
            self._dry_log("write_seed", season_id=season.season_id, period=period_index, seed=seed)
            return
        mapping = self._season_row_map(season)
        updates = [
            {"range": f"{_col_letter(COL_SEED)}{row}", "values": [[seed]]}
            for (p, _), row in mapping.items() if p == period_index
        ]
        if updates:
            ws.batch_update(updates)
        log.info("Wrote seed %s to %d rows (season %s, period %d)", seed, len(updates),
                 season.season_id, period_index)

    def write_seed_requested(
        self, season: SeasonSpec, period_index: int, discord_id: int, at: datetime
    ) -> None:
        """Write the FIRST seed-request timestamp for one participant."""
        self.ensure_season_rows(season)
        ws = self.get_or_create_season_tab(season.season_id)
        if ws is None:
            self._dry_log("write_seed_requested", season_id=season.season_id,
                          period=period_index, discord_id=discord_id, at=iso_utc(at))
            return
        row = self._target_row(season, period_index, discord_id)
        cell_ref = f"{_col_letter(COL_SEED_REQUESTED_AT)}{row}"
        existing = ws[cell_ref].value
        if existing:
            return  # first-request timestamp already recorded
        ws.update_acell(cell_ref, iso_utc(at))

    def write_submission(
        self,
        season: SeasonSpec,
        period_index: int,
        discord_id: int,
        at: datetime,
        run_time: str,
        video_url: str,
    ) -> None:
        self.ensure_season_rows(season)
        ws = self.get_or_create_season_tab(season.season_id)
        if ws is None:
            self._dry_log("write_submission", season_id=season.season_id,
                          period=period_index, discord_id=discord_id, at=iso_utc(at),
                          run_time=run_time, video=video_url)
            return
        row = self._target_row(season, period_index, discord_id)
        ws.batch_update([
            {"range": f"{_col_letter(COL_SUBMITTED_AT)}{row}", "values": [[iso_utc(at)]]},
            {"range": f"{_col_letter(COL_RUN_TIME)}{row}", "values": [[run_time]]},
            {"range": f"{_col_letter(COL_VIDEO)}{row}", "values": [[video_url]]},
        ])
        log.info("Wrote submission for discord id %d (season %s, period %d)",
                 discord_id, season.season_id, period_index)
