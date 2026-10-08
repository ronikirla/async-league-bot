"""Regression tests for the season sheet row mapping and pre-creation.

These drive the REAL ``SheetService.ensure_season_rows`` against an
in-memory fake worksheet. Dry-run mode skips all worksheet calls, so the
row mapping was previously untested — which let a discord-id-ordered
mapping mislabel pre-created rows on the live sheet (2026-10-04: new
registrants' rows kept the previous registrant's name and discord id).
"""
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from periods import SeasonSpec
from sheets import (
    COL_DISCORD_ID,
    COL_PARTICIPANT,
    COL_PERIOD_END,
    COL_PERIOD_START,
    COL_SEED,
    NUM_COLUMNS,
    SheetService,
)
from util import iso_utc


class FakeWorksheet:
    """Minimal gspread worksheet stand-in: a grid addressed like ``A1:J5``."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]  # row 1 is the header

    def _grow(self, num_rows):
        while len(self.rows) < num_rows:
            self.rows.append([""] * NUM_COLUMNS)

    def get_all_values(self):
        self._grow(1)
        width = max(len(r) for r in self.rows)
        return [r + [""] * (width - len(r)) for r in self.rows]

    def batch_update(self, updates):
        for upd in updates:
            first, _, last = upd["range"].partition(":")
            last = last or first
            r1, c1 = self._cell(first)
            r2, c2 = self._cell(last)
            self._grow(r2)
            for i, row_values in enumerate(upd["values"]):
                row = self.rows[r1 - 1 + i]
                for j, value in enumerate(row_values):
                    row[c1 - 1 + j] = str(value)

    @staticmethod
    def _cell(ref: str) -> tuple[int, int]:
        """'A10' -> (row 10, col 1)."""
        letters = "".join(ch for ch in ref if ch.isalpha())
        digits = "".join(ch for ch in ref if ch.isdigit())
        col = 0
        for ch in letters:
            col = col * 26 + (ord(ch.upper()) - 64)
        return int(digits), col


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep the dry-run log file inside the temp dir
    db = Database(str(tmp_path / "league.db"))
    config = Config(
        token="x",
        guild_id=1,
        admin_ids=(1,),
        spreadsheet_id="x",
        service_account_file="x",
        participant_role_name="League Participant",
        seed_not_done_role_name="League Seed Not Done",
        dry_run=True,
    )
    sheets = SheetService(config, db)
    ws = FakeWorksheet()
    # Bypass Google: hand ensure_season_rows our in-memory worksheet.
    monkeypatch.setattr(sheets, "get_or_create_season_tab", lambda season_id: ws)
    yield db, sheets, ws
    db.close()


def _make_season(db, num_periods=2) -> SeasonSpec:
    start = datetime(2026, 10, 11, tzinfo=timezone.utc)
    season_id = db.create_season(86400, num_periods, start.isoformat())
    return SeasonSpec(
        season_id=season_id,
        start_at=start,
        period_length=timedelta(days=1),
        num_periods=num_periods,
    )


def _register(db, discord_id: int, name: str, at: str) -> None:
    """Register with an explicit registered_at_utc (registrations in the
    same second would otherwise tie-break by discord id)."""
    db.add_participant(discord_id, name)
    db.execute(
        "UPDATE participants SET registered_at_utc = ? WHERE discord_id = ?",
        (at, discord_id),
    )


def test_ensure_season_rows_uses_registration_order(env):
    """Rows must be laid out in REGISTRATION order, not discord-id order.

    Regression (2026-10-04): the row mapping sorted participants by discord
    id, so a new registrant with a smaller id was mapped onto an existing
    participant's already-written rows, and the merge-only write kept the
    old occupant's labels — e.g. halqery's rows showed "Shady".
    """
    db, sheets, ws = env
    spec = _make_season(db, num_periods=2)

    # Shady registers first with a MIDDLE discord id, then halqery with a
    # SMALLER id — the exact shape of the live incident.
    _register(db, 300, "Shady", "2026-10-04T19:57:00+00:00")
    sheets.ensure_season_rows(spec)
    _register(db, 100, "halqery", "2026-10-04T19:57:30+00:00")
    sheets.ensure_season_rows(spec)

    # One block per participant in registration order, correctly labeled.
    names = [r[COL_PARTICIPANT - 1] for r in ws.rows[1:]]
    assert names == ["Shady", "Shady", "halqery", "halqery"]
    ids = [r[COL_DISCORD_ID - 1] for r in ws.rows[1:]]
    assert ids == ["300", "300", "100", "100"]
    # A third, later registration is appended at the bottom and never
    # relabels or shifts the existing blocks.
    _register(db, 200, "bleddark", "2026-10-04T19:57:50+00:00")
    sheets.ensure_season_rows(spec)
    names = [r[COL_PARTICIPANT - 1] for r in ws.rows[1:]]
    assert names == ["Shady", "Shady", "halqery", "halqery", "bleddark", "bleddark"]


def test_ensure_season_rows_repairs_mislabeled_rows(env):
    """Re-running ensure_season_rows must repair wrong identity cells.

    The live sheet from 2026-10-04 had blocks labeled with the previous
    registrant's name and discord id. The repair rewrites the identity
    columns from the row mapping while preserving dynamic cells.
    """
    db, sheets, ws = env
    spec = _make_season(db, num_periods=2)
    _register(db, 300, "Shady", "2026-10-04T19:57:00+00:00")
    _register(db, 100, "halqery", "2026-10-04T19:57:30+00:00")

    # A sheet in the corrupted state the old bug produced: halqery's block
    # (rows 4-5) carries Shady's labels; Shady's real row already has a seed.
    ws.rows = [
        ["Round", "Round Start (UTC)", "Round End (UTC)", "Participant", "Discord ID",
         "Seed", "Seed Requested At (UTC)", "Submitted At (UTC)", "Run Time (s)", "Video"],
        ["1", "junk", "junk", "Shady", "300", "42", "", "", "", ""],
        ["2", "junk", "junk", "Shady", "300", "", "", "", "", ""],
        ["1", "junk", "junk", "Shady", "300", "", "", "", "", ""],
        ["2", "junk", "junk", "Shady", "300", "", "", "", "", ""],
    ]
    sheets.ensure_season_rows(spec)

    # halqery's block is repaired: name, discord id, and the period window.
    assert ws.rows[3][COL_PARTICIPANT - 1] == "halqery"
    assert ws.rows[3][COL_DISCORD_ID - 1] == "100"
    assert ws.rows[4][COL_PARTICIPANT - 1] == "halqery"
    assert ws.rows[4][COL_DISCORD_ID - 1] == "100"
    assert ws.rows[3][COL_PERIOD_START - 1] == iso_utc(spec.period_start(1))
    assert ws.rows[3][COL_PERIOD_END - 1] == iso_utc(spec.period_end(1))
    # Shady's block keeps its identity and the already-written seed.
    assert ws.rows[1][COL_PARTICIPANT - 1] == "Shady"
    assert ws.rows[1][COL_DISCORD_ID - 1] == "300"
    assert ws.rows[1][COL_SEED - 1] == "42"
    assert ws.rows[1][COL_PERIOD_START - 1] == iso_utc(spec.period_start(1))


def test_ensure_season_rows_prefers_server_nickname(env):
    """When a name resolver is wired in, the sheet shows the server
    nickname instead of the name stored at registration."""
    db, sheets, ws = env
    spec = _make_season(db, num_periods=2)
    _register(db, 300, "Shady", "2026-10-04T19:57:00+00:00")
    sheets.name_resolver = lambda discord_id: {300: "ShadyNick"}.get(discord_id)

    sheets.ensure_season_rows(spec)

    names = [r[COL_PARTICIPANT - 1] for r in ws.rows[1:]]
    assert names == ["ShadyNick", "ShadyNick"]


def test_ensure_season_rows_falls_back_when_nickname_unknown(env):
    """A member the resolver cannot resolve (left the server, not cached)
    keeps the name stored at registration."""
    db, sheets, ws = env
    spec = _make_season(db, num_periods=2)
    _register(db, 300, "Shady", "2026-10-04T19:57:00+00:00")
    _register(db, 100, "halqery", "2026-10-04T19:57:30+00:00")
    # Only Shady is still on the server.
    sheets.name_resolver = lambda discord_id: "ShadyNick" if discord_id == 300 else None

    sheets.ensure_season_rows(spec)

    names = [r[COL_PARTICIPANT - 1] for r in ws.rows[1:]]
    assert names == ["ShadyNick", "ShadyNick", "halqery", "halqery"]


def test_ensure_season_rows_picks_up_nickname_change(env):
    """Identity columns are authoritative, so a nickname change is
    refreshed on the next ensure_season_rows run."""
    db, sheets, ws = env
    spec = _make_season(db, num_periods=1)
    _register(db, 300, "Shady", "2026-10-04T19:57:00+00:00")
    sheets.name_resolver = lambda discord_id: "OldNick"
    sheets.ensure_season_rows(spec)

    sheets.name_resolver = lambda discord_id: "NewNick"
    sheets.ensure_season_rows(spec)

    names = [r[COL_PARTICIPANT - 1] for r in ws.rows[1:]]
    assert names == ["NewNick"]


def test_ensure_season_rows_survives_resolver_error(env):
    """A failing resolver must not break the sheet write; the stored name
    is used instead."""
    db, sheets, ws = env
    spec = _make_season(db, num_periods=1)
    _register(db, 300, "Shady", "2026-10-04T19:57:00+00:00")

    def boom(discord_id: int) -> str:
        raise RuntimeError("member cache unavailable")

    sheets.name_resolver = boom
    sheets.ensure_season_rows(spec)

    names = [r[COL_PARTICIPANT - 1] for r in ws.rows[1:]]
    assert names == ["Shady"]
