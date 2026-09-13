"""All league commands.

- ``/league`` — participant commands, visible to everyone.
- ``/league_admin`` — admin commands, hidden from members without
  Administrator-equivalent permissions (the bot auto-creates a
  ``League Admin`` role with the Administrator permission for this
  purpose). An in-code :func:`admin_only` check is the final gate.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from periods import current_season_state
from roles import ADMIN_GROUP_PERMISSION, ADMIN_ROLE_NAME
from service import LeagueError, LeagueService
from util import discord_timestamp


def admin_only():
    """Decorator: restrict a command to configured admin user ids or holders
    of the League Admin role."""

    async def predicate(interaction: app_commands.Interaction) -> bool:
        bot = interaction.client
        if bot.config.is_admin(interaction.user.id):
            return True
        member = interaction.guild.get_member(interaction.user.id)
        if member is not None and any(r.name == ADMIN_ROLE_NAME for r in member.roles):
            return True
        await interaction.response.send_message(
            "❌ You are not a league admin.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)


class League(commands.Cog):
    """Async speedrun league commands."""

    def __init__(self, bot):
        self.bot = bot
        self.service: LeagueService = bot.service

    # -- groups --------------------------------------------------------
    league = app_commands.Group(name="league", description="Async speedrun league commands")

    # Hidden from members without the gating permission (the bot grants it
    # via the League Admin role to the league's admins).
    league_admin = app_commands.Group(
        name="league_admin",
        description="Admin commands for the async speedrun league (admins only)",
        default_permissions=ADMIN_GROUP_PERMISSION,
    )

    # -- helpers -----------------------------------------------------------
    async def sync_seed_not_done(self, guild: discord.Guild, user: discord.User) -> None:
        """Grant/revoke the seed-not-done role to match the database state."""
        state = current_season_state(self.bot.db)
        if state is None or not state.in_season or state.period_index is None:
            return
        member = guild.get_member(user.id)
        if member is None or not isinstance(member, discord.Member):
            return
        submitted = self.bot.db.has_submitted(state.season.season_id, state.period_index, user.id)
        await self.bot.roles.sync_seed_not_done(guild, member, should_have=not submitted)

    # -- participants (visible to everyone) --------------------------------

    @league.command(name="register", description="Register as a league participant")
    async def register(self, interaction: app_commands.Interaction):
        # Deferring keeps us within Discord's 3-second window when sheet
        # pre-creation happens in the same call.
        await interaction.response.defer(ephemeral=True)
        try:
            message = await asyncio.to_thread(self.service.register, interaction.user)
        except LeagueError as exc:
            message = f"❌ {exc}"
        else:
            await self.bot.roles.grant_participant(interaction.guild, interaction.user)
            # If the season is already in an active round, mark the seed
            # as not done (they have not submitted yet).
            state = current_season_state(self.bot.db)
            if (
                state is not None
                and state.in_season
                and state.period_index is not None
                and not self.bot.db.has_submitted(
                    state.season.season_id, state.period_index, interaction.user.id
                )
            ):
                await self.bot.roles.grant_seed_not_done(interaction.guild, interaction.user)
        await interaction.followup.send(message)

    @league.command(name="unregister", description="Unregister as a league participant")
    async def unregister(self, interaction: app_commands.Interaction):
        await interaction.response.defer(ephemeral=True)
        was_registered = self.bot.db.is_participant(interaction.user.id)
        try:
            message = await asyncio.to_thread(self.service.unregister, interaction.user)
        except LeagueError as exc:
            message = f"❌ {exc}"
        # Always make sure the league roles are gone (even if the DB entry
        # was already cleared, e.g. by the season-end cleanup).
        await self.bot.roles.revoke_participant(interaction.guild, interaction.user)
        if not was_registered and "closed" not in message.lower():
            message = f"League roles removed. (You were not registered: {message})"
        await interaction.followup.send(message)

    @league.command(name="seed", description="Request the RNG seed for the current round")
    async def seed(self, interaction: app_commands.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(self.service.request_seed, interaction.user)
        except LeagueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"🎲 **Seed for the current round:** `{result.seed}`\n"
            "Keep it to yourself until you have submitted your time.",
            ephemeral=True,
        )
        # Ensure the seed-not-done role is present (they have not submitted yet).
        state = current_season_state(self.bot.db)
        if (
            state is not None
            and state.in_season
            and state.period_index is not None
            and not self.bot.db.has_submitted(
                state.season.season_id, state.period_index, interaction.user.id
            )
        ):
            await self.bot.roles.grant_seed_not_done(interaction.guild, interaction.user)

    @league.command(name="submit", description="Submit your run time and video for the current round")
    @app_commands.describe(
        time="Your run time: M:SS or H:MM:SS with optional .mmm milliseconds (e.g. 12:34.567)",
        video="Link to the video of your run (http/https)",
    )
    async def submit(self, interaction: app_commands.Interaction, time: str, video: str):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(self.service.submit, interaction.user, time, video)
        except LeagueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        embed = discord.Embed(
            title="Submission recorded ✅",
            description=(
                f"**Time:** `{result.run_time}`\n"
                f"**Video:** {video.strip()}\n"
                f"**Submitted at:** {discord_timestamp(result.submitted_at)}"
            ),
            colour=discord.Colour.green(),
        )
        await interaction.followup.send(embed=embed)
        await self.bot.roles.revoke_seed_not_done(interaction.guild, interaction.user)

    # -- admin (hidden from non-admins) ------------------------------------

    @league_admin.command(
        name="setup",
        description="Create the league roles and optionally hide the seed channel",
    )
    @app_commands.describe(seed_channel="The seed discussion channel to hide from unsubmitted participants")
    @admin_only()
    async def setup(self, interaction: app_commands.Interaction,
                    seed_channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.service.setup(interaction.guild, seed_channel)
        except LeagueError as exc:
            message = f"❌ {exc}"
        await interaction.followup.send(message)

    @league_admin.command(name="create_season", description="Start a new league season")
    @app_commands.describe(
        round_length="Length of each round, e.g. 7d, 12h, 1d12h (d=days, h=hours, m=minutes)",
        rounds="Number of rounds in the season",
        start="Start time of the first round: ISO 8601 (UTC if no zone given) or 'now'",
    )
    @admin_only()
    async def create_season(self, interaction: app_commands.Interaction,
                            round_length: str, rounds: int, start: str = "now"):
        # Google Sheets calls can take several seconds; defer so we stay
        # within Discord's 3-second response window.
        await interaction.response.defer(ephemeral=True)
        try:
            state = await asyncio.to_thread(
                self.service.create_season, round_length, rounds, start
            )
        except LeagueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        spec = state.season
        # Grant the seed-not-done role to registered participants of an
        # already-active round (they have not submitted yet).
        if state.in_season and state.period_index is not None:
            for member in interaction.guild.members:
                if isinstance(member, discord.Member) and self.bot.db.is_participant(member.id):
                    if not self.bot.db.has_submitted(spec.season_id, state.period_index, member.id):
                        await self.bot.roles.grant_seed_not_done(interaction.guild, member)
        embed = discord.Embed(
            title=f"Season {spec.season_id} created",
            description=(
                f"**Round length:** {spec.period_length}\n"
                f"**Number of rounds:** {spec.num_periods}\n"
                f"**Start:** {discord_timestamp(spec.start_at)}\n"
                f"**End:** {discord_timestamp(spec.end_at)}\n\n"
                "Sheet rows were pre-created for all registered participants."
            ),
            colour=discord.Colour.green(),
        )
        await interaction.followup.send(embed=embed)

    @league_admin.command(name="season_info", description="Show the current season and active round")
    @admin_only()
    async def season_info(self, interaction: app_commands.Interaction):
        # Clean up registrations from any ended season before reporting.
        note = self.service.cleanup_ended_season()
        if note is not None:
            await self.bot.roles.strip_league_roles(interaction.guild)
        message = self.service.season_info()
        if note:
            message = f"🧹 {note}\n\n{message}"
        await interaction.response.send_message(message)

    @league_admin.command(name="add_participant", description="Register a user as a league participant")
    @app_commands.describe(user="The user to register")
    @admin_only()
    async def add_participant(self, interaction: app_commands.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        try:
            message = await asyncio.to_thread(self.service.add_participant, user)
        except LeagueError as exc:
            message = f"❌ {exc}"
        else:
            member = interaction.guild.get_member(user.id)
            if member:
                await self.bot.roles.grant_participant(interaction.guild, member)
                await self.sync_seed_not_done(interaction.guild, user)
        await interaction.followup.send(message)

    @league_admin.command(name="remove_participant", description="Remove a user from the league")
    @app_commands.describe(user="The user to remove")
    @admin_only()
    async def remove_participant(self, interaction: app_commands.Interaction, user: discord.User):
        message = self.service.remove_participant(user)
        member = interaction.guild.get_member(user.id)
        if member:
            await self.bot.roles.revoke_participant(interaction.guild, member)
        await interaction.response.send_message(message)
