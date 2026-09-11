"""Admin commands: season lifecycle, role setup, participant management."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from periods import current_season_state
from service import LeagueError, LeagueService


def admin_only():
    """Decorator: restrict a command to configured admin user ids."""

    async def predicate(interaction: app_commands.Interaction) -> bool:
        bot = interaction.client
        if bot.config.is_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            "❌ You are not a league admin.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.service: LeagueService = bot.service

    @app_commands.command(name="setup", description="Create the league roles and optionally hide the seed channel")
    @app_commands.describe(seed_channel="The seed discussion channel to hide from unsubmitted participants")
    @admin_only()
    async def setup(self, interaction: app_commands.Interaction,
                    seed_channel: discord.TextChannel | None = None):
        try:
            message = await self.service.setup(interaction.guild, seed_channel)
        except LeagueError as exc:
            message = f"❌ {exc}"
        await interaction.response.send_message(message)

    @app_commands.command(name="create_season", description="Start a new league season")
    @app_commands.describe(
        period_length="Length of each period, e.g. 7d, 12h, 1d12h (d=days, h=hours, m=minutes)",
        periods="Number of periods in the season",
        start="Start time of the first period: ISO 8601 (UTC if no zone given) or 'now'",
    )
    @admin_only()
    async def create_season(self, interaction: app_commands.Interaction,
                            period_length: str, periods: int, start: str = "now"):
        try:
            state = self.service.create_season(period_length, periods, start)
        except LeagueError as exc:
            await interaction.response.send_message(f"❌ {exc}")
            return
        spec = state.season
        embed = discord.Embed(
            title=f"Season {spec.season_id} created",
            description=(
                f"**Period length:** {spec.period_length}\n"
                f"**Number of periods:** {spec.num_periods}\n"
                f"**Start (UTC):** {state.period_start.isoformat() if state.period_start else spec.start_at.isoformat()}\n"
                f"**End (UTC):** {spec.end_at.isoformat()}\n\n"
                "Sheet rows were pre-created for all registered participants."
            ),
            colour=discord.Colour.green(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="season_info", description="Show the current season and active period")
    @admin_only()
    async def season_info(self, interaction: app_commands.Interaction):
        await interaction.response.send_message(self.service.season_info())

    @app_commands.command(name="add_participant", description="Register a user as a league participant")
    @app_commands.describe(user="The user to register")
    @admin_only()
    async def add_participant(self, interaction: app_commands.Interaction, user: discord.User):
        message = self.service.add_participant(user)
        member = interaction.guild.get_member(user.id)
        if member:
            await self.bot.roles.grant_participant(interaction.guild, member)
            state = current_season_state(self.bot.db)
            if state and state.in_season and state.period_index is not None:
                if not self.bot.db.has_submitted(state.season.season_id, state.period_index, user.id):
                    await self.bot.roles.grant_seed_not_done(interaction.guild, member)
        await interaction.response.send_message(message)

    @app_commands.command(name="remove_participant", description="Remove a user from the league")
    @app_commands.describe(user="The user to remove")
    @admin_only()
    async def remove_participant(self, interaction: app_commands.Interaction, user: discord.User):
        message = self.service.remove_participant(user)
        if user.id != interaction.user.id:
            member = interaction.guild.get_member(user.id)
            if member:
                await self.bot.roles.revoke_participant(interaction.guild, member)
        await interaction.response.send_message(message)
