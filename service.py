"""High-level league operations shared by the cogs.

Each method is one user-visible action. Errors are raised as
:class:`LeagueError` with a user-friendly message that the cogs relay
back to Discord.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord

from config import Config
from db import Database
from periods import (
    PeriodError,
    SeasonSpec,
    SeasonState,
    current_season_state,
    format_run_time,
    parse_period_length,
    parse_run_time,
    parse_start_time,
)
from roles import RoleManager
from scheduler import ScheduledEvent, Scheduler
from sheets import SheetsError, SheetService
from util import discord_timestamp, format_duration, iso_utc, is_http_url, now_utc

log = logging.getLogger("league")

MAX_SEED = 10**10  # seeds are 0 .. 9999999999


class LeagueError(Exception):
    """User-facing error with a message safe to send to Discord."""


@dataclass
class SeedResult:
    seed: str
    first_request: bool  # True if this call recorded the first request


@dataclass
class SubmitResult:
    run_time: str
    submitted_at: datetime


class LeagueService:
    def __init__(self, config: Config, db: Database, roles: RoleManager, sheets: SheetService):
        self.config = config
        self.db = db
        self.roles = roles
        self.sheets = sheets
        self.scheduler = Scheduler(db)

    # -- helpers ---------------------------------------------------------
    def require_state(self, now: datetime | None = None) -> SeasonState:
        state = current_season_state(self.db, now or now_utc())
        if state is None:
            raise LeagueError("No season has been created yet.")
        return state

    def require_active(self, now: datetime | None = None) -> tuple[SeasonState, int]:
        state = self.require_state(now)
        if not state.in_season or state.period_index is None:
            if state.season.end_at <= (now or now_utc()):
                raise LeagueError("The season has ended.")
            raise LeagueError("The season has not started yet.")
        return state, state.period_index

    def require_participant(self, member: discord.Member) -> None:
        if not self.db.is_participant(member.id):
            raise LeagueError(
                "You are not a registered participant. Use `/league register` to join the league."
            )

    def _spec(self, state: SeasonState) -> SeasonSpec:
        return state.season

    # -- scheduled events (announcements + role changes) -------------------
    async def on_scheduled_event(self, guild: discord.Guild, event: ScheduledEvent) -> bool:
        """Handle one scheduler event: post the announcement and do the
        role changes that belong to that moment.

        Past events are replayed by the scheduler at boot, so a restart
        never skips an announcement or a role change.
        """
        row = self.db.get_latest_season()
        if row is None or int(row["id"]) != event.season_id:
            return  # a newer season replaced this one; ignore stale events
        spec = SeasonSpec(
            season_id=int(row["id"]),
            start_at=datetime.fromisoformat(row["start_at_utc"]),
            period_length=timedelta(seconds=int(row["period_length_seconds"])),
            num_periods=int(row["num_periods"]),
        )

        if event.kind == "period_start":
            index = event.period_index
            assert index is not None
            await self.roles.on_round_start(guild, spec, index)
            # Round 1's start doubles as the season-start announcement.
            tail = (
                "\nRegistration is closed while the season runs."
                if index == 1
                else ""
            )
            return await self.roles.announce(
                guild,
                f"🟢 **Season {spec.season_id}, Round "
                f"{index}/{spec.num_periods} has started!**\n"
                f"Ends: {discord_timestamp(spec.period_end(index))}\n"
                f"Use `/league seed` to get the seed and `/league submit` "
                f"when you are done."
                + tail,
            )

        elif event.kind == "period_reminder":
            index = event.period_index
            assert index is not None
            not_done = self.db.unsubmitted_ids(spec.season_id, index)
            if not_done:
                # Ping the seed-not-done role: its holders are exactly the
                # participants who have not reported this round yet.
                role = await self.roles.seed_not_done_role(guild)
                return await self.roles.announce(
                    guild,
                    f"⏰ {role.mention} **24 hours left in Season "
                    f"{spec.season_id}, round {index}/{spec.num_periods}!**\n"
                    f"Ends: {discord_timestamp(spec.period_end(index))}\n"
                    "Submit with `/league submit`, or `/league dnf` if you "
                    "can't finish.",
                )
            else:
                return await self.roles.announce(
                    guild,
                    f"🎉 Everyone has reported for Season {spec.season_id} "
                    f"round {index}/{spec.num_periods}, which ends "
                    f"{discord_timestamp(spec.period_end(index))}.",
                )

        elif event.kind == "season_end":
            await self.roles.on_round_end(guild, spec, spec.num_periods)
            count = self.db.clear_participants()
            log.info("Season %d ended: cleared %d registration(s)", spec.season_id, count)
            return await self.roles.announce(
                guild,
                f"🏆 **Season {spec.season_id} has ended!**\n"
            )

        return False  # unknown event kind (unreachable with current kinds)

    def start_scheduler(self, get_guild) -> None:
        """Arm all timers for the latest season and replay missed events.

        ``get_guild`` is called for every event so the guild is resolved
        fresh (it may not be cached the instant the bot boots).

        Fully handled events are recorded in the database, and catch-up
        skips events already recorded — so a restart replays only what was
        truly missed, never an announcement that was already posted.
        """

        async def dispatch(event: ScheduledEvent) -> None:
            guild = get_guild()
            if guild is None:
                log.warning("Guild not available; skipping %s event", event.kind)
                return
            if self.db.is_event_dispatched(
                event.season_id, event.kind, event.period_index
            ):
                log.info(
                    "Skipping %s event for season %s round %s (already dispatched)",
                    event.kind, event.season_id, event.period_index,
                )
                return
            handled = await self.on_scheduled_event(guild, event)
            if handled:
                self.db.mark_event_dispatched(
                    event.season_id, event.kind, event.period_index
                )

        self.scheduler.start(dispatch)

    def reschedule(self) -> None:
        """Recompute timers after a season is created.

        Never raises: the season is already committed at this point, so a
        failure to arm the timers is logged but must not fail the command.
        """
        try:
            self.scheduler.reschedule()
        except Exception:
            log.exception("Failed to re-arm season timers")

    def _ensure_records(self, season: SeasonSpec, discord_id: int) -> None:
        """Create DB period records for a participant (idempotent)."""
        for period_index in range(1, season.num_periods + 1):
            self.db.create_record(season.season_id, period_index, discord_id)

    # -- admin actions ----------------------------------------------------
    async def setup(self, guild: discord.Guild, seed_channel: discord.TextChannel | None) -> str:
        """Create both roles and (optionally) wire up the seed channel."""
        participant_role = await self.roles.participant_role(guild)
        not_done_role = await self.roles.seed_not_done_role(guild)
        lines = [
            f"Created/verified roles: **{participant_role.name}** and **{not_done_role.name}**.",
        ]
        if seed_channel is not None:
            await self.roles.apply_seed_channel_permissions(guild, seed_channel)
            lines.append(
                f"Applied permission overrides to **#{seed_channel.name}**: "
                f"visible to everyone EXCEPT members with **{not_done_role.name}**."
            )
        else:
            lines.append(
                "Seed channel not set. Run `/league_admin setup` with `seed_channel` to hide the seed "
                "discussion from unsubmitted participants."
            )
        return "\n".join(lines)

    def create_season(
        self, period_length_text: str, num_periods: int, start_text: str
    ) -> SeasonState:
        try:
            period_length = parse_period_length(period_length_text)
        except PeriodError as exc:
            raise LeagueError(str(exc)) from exc
        if num_periods < 1:
            raise LeagueError("Number of rounds must be at least 1.")
        if num_periods > 365:
            raise LeagueError("Number of rounds must be at most 365.")
        try:
            start_at = parse_start_time(start_text, now_utc())
        except PeriodError as exc:
            raise LeagueError(str(exc)) from exc

        season_id = self.db.create_season(int(period_length.total_seconds()), num_periods, iso_utc(start_at))
        log.info("Created season %d: %s x %d periods starting %s",
                 season_id, period_length, num_periods, iso_utc(start_at))

        state = current_season_state(self.db)
        assert state is not None and state.season.season_id == season_id
        # Arm the exact timers for this season (replacing any previous ones)
        # before any optional follow-up work that could fail.
        self.reschedule()
        # Pre-create one DB record per (participant, period).
        for p in self.db.list_participants():
            self._ensure_records(state.season, int(p["discord_id"]))
        # Pre-create sheet rows for all registered participants.
        try:
            self.sheets.ensure_season_rows(state.season)
        except SheetsError as exc:
            raise LeagueError(f"Season created, but the Google sheet could not be updated: {exc}") from exc
        return state

    async def announce_signups_open(self, guild: discord.Guild, state: SeasonState) -> bool:
        """Post the sign-ups announcement right after creating a season whose
        first round has not started yet.

        Returns True when the announcement was posted. A season that starts
        in the past does not get this message — its season-start
        announcement is replayed by the scheduler's catch-up instead.
        """
        spec = state.season
        return await self.roles.announce(
            guild,
            f"📣 **Sign-ups are open for Season {spec.season_id}!**\n"
            f"Round length: {format_duration(spec.period_length)} · "
            f"Rounds: {spec.num_periods}\n"
            f"Season starts: {discord_timestamp(spec.start_at)}\n"
            "Register with `/league register` before it begins!",
        )

    def season_info(self) -> str:
        state = current_season_state(self.db)
        if state is None:
            return "No season has been created yet."
        spec = state.season
        now = now_utc()
        lines = [
            f"**Season {spec.season_id}**",
            f"Round length: `{format_duration(spec.period_length)}`",
            f"Rounds: {spec.num_periods}",
            f"Season start: {discord_timestamp(spec.start_at)}",
            f"Season end: {discord_timestamp(spec.end_at)}",
            f"Registered participants: {len(self.db.list_participants())}",
        ]
        if state.in_season and state.period_index is not None:
            lines.append(
                f"Active round: **{state.period_index}/{spec.num_periods}** "
                f"({discord_timestamp(state.period_start)} → {discord_timestamp(state.period_end)})"
            )
            seed = self.db.get_period_seed(spec.season_id, state.period_index)
            lines.append(f"Current round seed: `{seed or 'not set (no one has requested it yet)'}`")
        elif now < spec.start_at:
            lines.append(f"Season starts in {format_duration(spec.start_at - now)}.")
        else:
            lines.append("Season has ended.")
        return "\n".join(lines)

    def add_participant(self, user: discord.User) -> str:
        name = getattr(user, "global_name", None) or user.name
        created = self.db.add_participant(int(user.id), name)
        if not created:
            return f"<@{user.id}> is already registered."
        # Pre-create DB records + sheet rows for the active season (if any).
        state = current_season_state(self.db)
        if state is not None:
            self._ensure_records(state.season, int(user.id))
            try:
                self.sheets.ensure_season_rows(state.season)
            except SheetsError as exc:
                return f"Registered <@{user.id}>, but the sheet update failed: {exc}"
        return f"Registered <@{user.id}> as a league participant."

    def remove_participant(self, user: discord.User) -> str:
        if not self.db.is_participant(int(user.id)):
            return f"<@{user.id}> is not a registered participant."
        state = current_season_state(self.db)
        if state is not None:
            self.db.delete_records_for(state.season.season_id, int(user.id))
        self.db.remove_participant(int(user.id))
        return f"Removed <@{user.id}> from the league."

    # -- registration rules ----------------------------------------------------
    def registration_open(self) -> None:
        """Registration and unregistration are only allowed while no round is active."""
        state = current_season_state(self.db)
        if state is not None and state.in_season:
            raise LeagueError(
                "Registration is closed while the season is in progress. "
                "It re-opens once the season ends."
            )

    # -- participant actions -------------------------------------------------
    def register(self, member: discord.Member) -> str:
        self.registration_open()
        if self.db.is_participant(member.id):
            return f"Welcome back, {member.mention}! You are already registered."
        name = member.global_name or member.name
        self.db.add_participant(member.id, name)
        state = current_season_state(self.db)
        if state is not None:
            self._ensure_records(state.season, member.id)
            try:
                self.sheets.ensure_season_rows(state.season)
            except SheetsError as exc:
                return f"Registered, but the sheet update failed: {exc}"
        return f"Registered {member.mention} as a league participant. You can now use `/league seed` and `/league submit`."

    def unregister(self, member: discord.Member) -> str:
        self.registration_open()
        if not self.db.is_participant(member.id):
            return f"You are not registered, so there is nothing to do."
        state = current_season_state(self.db)
        if state is not None:
            self.db.delete_records_for(state.season.season_id, member.id)
        self.db.remove_participant(member.id)
        return f"You have been unregistered. You can use `/league register` again once the next season opens."

    def request_seed(self, member: discord.Member) -> SeedResult:
        self.require_participant(member)
        state, period_index = self.require_active()
        spec = self._spec(state)

        # Defensive: make sure this participant has a record for this period.
        self.db.create_record(spec.season_id, period_index, member.id)

        # One shared seed per period for the whole league.
        seed = self.db.get_period_seed(spec.season_id, period_index)
        if seed is None:
            seed = str(secrets.randbelow(MAX_SEED))
            self.db.set_record_seed(spec.season_id, period_index, seed)
            try:
                self.sheets.write_seed(spec, period_index, seed)
            except SheetsError as exc:
                raise LeagueError(
                    f"Seed {seed} was generated and recorded locally, "
                    f"but writing it to the sheet failed: {exc}"
                ) from exc
            log.info("Generated seed %s for season %s period %d", seed, spec.season_id, period_index)
        else:
            # Mid-period registration: make sure this participant's row has the seed too.
            self.db.set_record_seed(spec.season_id, period_index, seed)

        first_request = self.db.mark_seed_requested(spec.season_id, period_index, member.id)

        # Keep the sheet in sync with the DB-stored first-request timestamp.
        # The sheet cell is only ever filled when empty, so this is idempotent
        # and self-heals if a previous write was lost.
        record = self.db.record_exists(spec.season_id, period_index, member.id)
        stored_at = record["seed_requested_at_utc"] if record else None
        if stored_at:
            try:
                self.sheets.write_seed_requested(
                    spec, period_index, member.id, datetime.fromisoformat(stored_at)
                )
            except SheetsError as exc:
                raise LeagueError(
                    f"Seed requested, but logging to the sheet failed: {exc}"
                ) from exc

        return SeedResult(seed=seed, first_request=first_request)

    def submit(self, member: discord.Member, time_text: str, video_text: str) -> SubmitResult:
        self.require_participant(member)
        state, period_index = self.require_active()
        spec = self._spec(state)

        try:
            seconds = parse_run_time(time_text)
        except PeriodError as exc:
            raise LeagueError(str(exc)) from exc
        if not is_http_url(video_text.strip()):
            raise LeagueError("Video must be an http(s) link, e.g. a YouTube URL.")

        if self.db.has_submitted(spec.season_id, period_index, member.id) or \
                self.db.has_dnf(spec.season_id, period_index, member.id):
            # Self-heal: a previous sheet write may have been lost.
            self._sync_submission_to_sheet(spec, period_index, member.id, quiet=True)
            raise LeagueError(
                "You already reported this round (submitted or DNF). Reports are final."
            )

        self.db.create_record(spec.season_id, period_index, member.id)
        at = now_utc()
        stored = self.db.mark_submitted(
            spec.season_id, period_index, member.id, round(seconds, 3), video_text.strip()
        )
        if not stored:
            raise LeagueError("You already submitted a time for this round.")

        self._sync_submission_to_sheet(spec, period_index, member.id)
        return SubmitResult(run_time=format_run_time(seconds), submitted_at=at)

    def dnf(self, member: discord.Member) -> SubmitResult:
        """Mark the current round as DNF (did not finish) for the member."""
        self.require_participant(member)
        state, period_index = self.require_active()
        spec = self._spec(state)

        if self.db.has_dnf(spec.season_id, period_index, member.id):
            # Self-heal: a previous sheet write may have been lost.
            self._sync_submission_to_sheet(spec, period_index, member.id, quiet=True)
            raise LeagueError("You already marked this round as DNF. Reports are final.")
        if self.db.has_submitted(spec.season_id, period_index, member.id):
            raise LeagueError("You already submitted a time for this round. You cannot DNF it.")

        at = now_utc()
        stored = self.db.mark_dnf(spec.season_id, period_index, member.id)
        if not stored:
            raise LeagueError("You already reported this round (submitted or DNF).")

        self._sync_submission_to_sheet(spec, period_index, member.id)
        return SubmitResult(run_time="DNF", submitted_at=at)

    def _sync_submission_to_sheet(
        self, season: SeasonSpec, period_index: int, discord_id: int, quiet: bool = False
    ) -> None:
        """Write the DB-stored report to the sheet (idempotent self-heal)."""
        record = self.db.record_exists(season.season_id, period_index, discord_id)
        if record is None or record["submitted_at_utc"] is None:
            return
        dnf = int(record["dnf"] or 0) == 1
        run_time_value: float | str = "DNF" if dnf else float(record["run_time"])
        video_url = record["video_url"] or ""
        try:
            self.sheets.write_submission(
                season, period_index, discord_id,
                datetime.fromisoformat(record["submitted_at_utc"]),
                run_time_value, video_url,
            )
        except SheetsError as exc:
            if quiet:
                log.warning("Could not re-sync submission for %s to sheet: %s", discord_id, exc)
                return
            raise LeagueError(
                f"Your time was recorded locally, but the sheet update failed: {exc}"
            ) from exc
