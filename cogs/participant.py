"""Participant commands: registration, seed requests, submissions."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from periods import current_season_state
from service import LeagueError


class Participant(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.service = bot.service

    @app_commands.command(name="register", description="Register as a league participant")
    async def register(self, interaction: app_commands.Interaction):
        try:
            message = self.service.register(interaction.user)
        except LeagueError as exc:
            message = f"❌ {exc}"
        await interaction.response.send_message(message)

        await self.bot.roles.grant_participant(interaction.guild, interaction.user)
        state = current_season_state(self.bot.db)
        if state and state.in_season and state.period_index is not None:
            if not self.bot.db.has_submitted(state.season.season_id, state.period_index, interaction.user.id):
                await self.bot.roles.grant_seed_not_done(interaction.guild, interaction.user)

    @app_commands.command(name="seed", description="Request the RNG seed for the current period")
    async def seed(self, interaction: app_commands.Interaction):
        try:
            result = self.service.request_seed(interaction.user)
        except LeagueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎲 **Seed for the current period:** `{result.seed}`\n"
            "Keep it to yourself until you have submitted your time.",
            ephemeral=True,
        )
        state = current_season_state(self.bot.db)
        if state and state.in_season and state.period_index is not None:
            if not self.bot.db.has_submitted(state.season.season_id, state.period_index, interaction.user.id):
                await self.bot.roles.grant_seed_not_done(interaction.guild, interaction.user)

    @app_commands.command(name="submit", description="Submit your run time and video for the current period")
    @app_commands.describe(
        time="Your run time: M:SS or H:MM:SS with optional .mmm milliseconds (e.g. 12:34.567)",
        video="Link to the video of your run (http/https)",
    )
    async def submit(self, interaction: app_commands.Interaction, time: str, video: str):
        try:
            result = self.service.submit(interaction.user, time, video)
        except LeagueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        embed = discord.Embed(
            title="Submission recorded ✅",
            description=(
                f"**Time:** `{result.run_time}`\n"
                f"**Video:** {video.strip()}\n"
                f"**Submitted at (UTC):** {result.submitted_at.isoformat()}"
            ),
            colour=discord.Colour.green(),
        )
        await interaction.response.send_message(embed=embed)
        await self.bot.roles.revoke_seed_not_done(interaction.guild, interaction.user)
