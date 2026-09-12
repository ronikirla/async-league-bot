"""Role management for the league.

Two roles:
- **participant role** (e.g. ``League Participant``): gates ``/league seed`` and
  ``/league submit``. Granted on registration, removed on ``/league remove_participant``.
- **seed-not-done role** (e.g. ``League Seed Not Done``): granted while a
  registered participant has NOT submitted the active round. This role is
  the only permission deny on the seed discussion channel, so unsubmitted
  participants cannot see it (spoiler protection), while submitters and
  non-participants can.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import Guild, Member

from config import Config
from db import Database, PARTICIPANT_ROLE_KEY, SEED_NOT_DONE_ROLE_KEY, SEED_CHANNEL_KEY
from periods import SeasonState

log = logging.getLogger("league")


class RoleError(Exception):
    """Raised when a required role cannot be found or created."""


class RoleManager:
    def __init__(self, config: Config, db: Database):
        self._config = config
        self._db = db

    # -- role lookup / creation ----------------------------------------
    def _find_role(self, guild: Guild, name: str) -> Optional[discord.Role]:
        for role in guild.roles:
            if role.name == name:
                return role
        return None

    async def ensure_role(self, guild: Guild, name: str, settings_key: str) -> discord.Role:
        """Return the role by name, creating it if missing. Caches its id."""
        role = self._find_role(guild, name)
        if role is None:
            log.info("Creating role %r", name)
            role = await guild.create_role(
                name=name,
                colour=discord.Colour.default(),
                hoist=False,
                mentionable=True,
                reason="Created by async league bot",
            )
        self._db.set_setting(settings_key, str(role.id))
        return role

    async def participant_role(self, guild: Guild) -> discord.Role:
        return await self.ensure_role(guild, self._config.participant_role_name, PARTICIPANT_ROLE_KEY)

    async def seed_not_done_role(self, guild: Guild) -> discord.Role:
        return await self.ensure_role(guild, self._config.seed_not_done_role_name, SEED_NOT_DONE_ROLE_KEY)

    # -- membership -----------------------------------------------------
    async def grant_participant(self, guild: Guild, user: discord.User | discord.Member) -> None:
        member = self._as_member(guild, user)
        if member is None or not isinstance(member, Member):
            return
        role = await self.participant_role(guild)
        if role not in member.roles:
            await member.add_roles(role, reason="Registered for the league")
            log.info("Granted participant role to %s (%s)", member.display_name, member.id)

    async def revoke_participant(self, guild: Guild, user: discord.User | discord.Member) -> None:
        member = self._as_member(guild, user)
        if member is None or not isinstance(member, Member):
            return
        for key, name in ((PARTICIPANT_ROLE_KEY, self._config.participant_role_name),
                          (SEED_NOT_DONE_ROLE_KEY, self._config.seed_not_done_role_name)):
            role = self._find_role(guild, name)
            if role and role in member.roles:
                await member.remove_roles(role, reason="Removed from the league")
        log.info("Revoked league roles from %s (%s)", member.display_name, member.id)

    async def grant_seed_not_done(self, guild: Guild, member: Member) -> None:
        role = await self.seed_not_done_role(guild)
        if role not in member.roles:
            await member.add_roles(role, reason="Seed not yet submitted this round")

    async def revoke_seed_not_done(self, guild: Guild, member: Member) -> None:
        role = self._find_role(guild, self._config.seed_not_done_role_name)
        if role and role in member.roles:
            await member.remove_roles(role, reason="Submitted this round")

    async def sync_seed_not_done(self, guild: Guild, member: Member, should_have: bool) -> None:
        """Grant or revoke the seed-not-done role to match ``should_have``."""
        if should_have:
            await self.grant_seed_not_done(guild, member)
        else:
            await self.revoke_seed_not_done(guild, member)

    async def strip_league_roles(self, guild: Guild) -> None:
        """Remove both league roles from any member who has them (idempotent)."""
        names = {self._config.participant_role_name, self._config.seed_not_done_role_name}
        changed = 0
        for member in guild.members:
            if not isinstance(member, discord.Member):
                continue
            to_remove = [r for r in member.roles if r.name in names]
            if to_remove:
                await member.remove_roles(*to_remove, reason="Season ended (cleanup)")
                changed += 1
        if changed:
            log.info("Stripped league roles from %d member(s)", changed)

    @staticmethod
    def _as_member(guild: Guild, user: discord.User | discord.Member) -> Optional[discord.User | Member]:
        if isinstance(user, Member):
            return user
        return guild.get_member(user.id)

    # -- reconciliation -------------------------------------------------
    async def reconcile(self, guild: Guild, state: Optional[SeasonState]) -> None:
        """Bring the seed-not-done role in line with the database.

        A registered participant should have the role iff they have not
        submitted the active round. No-op when there is no active season
        (all members lose the role).
        """
        not_done_role = self._find_role(guild, self._config.seed_not_done_role_name)
        if not_done_role is None:
            return  # /setup has not run yet

        should_have: set[int] = set()
        if state is not None and state.in_season and state.period_index is not None:
            should_have = self._db.unsubmitted_ids(state.season.season_id, state.period_index)

        changed = 0
        for member in guild.members:
            if not isinstance(member, discord.Member):
                continue
            has_role = not_done_role in member.roles
            should = member.id in should_have
            if has_role and not should:
                await member.remove_roles(not_done_role, reason="Reconcile: submitted")
                changed += 1
            elif should and not has_role:
                await member.add_roles(not_done_role, reason="Reconcile: not submitted")
                changed += 1
        if changed:
            log.info("Reconciliation adjusted %d members", changed)

    # -- seed channel permissions ---------------------------------------
    async def apply_seed_channel_permissions(self, guild: Guild, channel: discord.TextChannel) -> None:
        """Apply the spoiler-protecting overrides to the seed discussion channel.

        - ``@everyone``: may view (default for a guild channel)
        - seed-not-done role: may NOT view (deny wins over the @everyone allow)
        """
        not_done_role = await self.seed_not_done_role(guild)
        everyone = guild.default_role

        # Overwrites must be keyed by Role objects (not int ids) in discord.py 2.x.
        overrides = {
            everyone: discord.PermissionOverwrite(view_channel=True),
            not_done_role: discord.PermissionOverwrite(view_channel=False),
        }
        await channel.edit(sync_permissions=False, overwrites=overrides,
                           reason="Seed discussion channel: hidden from unsubmitted participants")
        self._db.set_setting(SEED_CHANNEL_KEY, str(channel.id))
        log.info("Applied seed channel permission overrides on %r (id %s)", channel.name, channel.id)

    def get_seed_channel_id(self) -> Optional[int]:
        value = self._db.get_setting(SEED_CHANNEL_KEY)
        return int(value) if value else None
