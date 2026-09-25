"""Role management for the league.

Three roles:
- **participant role** (e.g. ``League Participant``): gates ``/league seed`` and
  ``/league submit``. Granted on registration, removed on ``/league_admin remove_participant``.
- **seed-not-done role** (e.g. ``League Seed Not Done``): granted while a
  registered participant has NOT submitted the active round. Granted at round
  start (inline with the round-start announcement) and removed on submission
  or when the round ends — there is no background reconciliation.
- **admin role** (``League Admin``): created by the bot at startup with the
  Administrator permission so that the ``/league_admin`` command group is
  only visible to the league's admins.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import Guild, Member

from config import Config
from db import (
    ANNOUNCE_CHANNEL_KEY,
    Database,
    PARTICIPANT_ROLE_KEY,
    SEED_NOT_DONE_ROLE_KEY,
    SEED_CHANNEL_KEY,
)
from periods import SeasonSpec

log = logging.getLogger("league")


class RoleError(Exception):
    """Raised when a required role cannot be found or created."""


ADMIN_ROLE_NAME = "League Admin"

# The permission that gates the /league_admin command group's visibility.
# It must be a permission the BOT itself holds (a bot cannot create or grant
# a role carrying a permission it lacks, unless it has Administrator).
# Manage Roles is sufficient and already granted to the bot. If the bot is
# given the Administrator permission, this can be switched to
# discord.Permissions(administrator=True) for a tighter gate.
ADMIN_GROUP_PERMISSION = discord.Permissions(manage_roles=True)


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

    async def ensure_role(self, guild: Guild, name: str, settings_key: str,
                          permissions: discord.Permissions | None = None) -> discord.Role:
        """Return the role by name, creating it if missing. Caches its id."""
        role = self._find_role(guild, name)
        if role is None:
            log.info("Creating role %r", name)
            role = await guild.create_role(
                name=name,
                colour=discord.Colour.default(),
                hoist=False,
                mentionable=True,
                permissions=permissions,
                reason="Created by async league bot",
            )
        self._db.set_setting(settings_key, str(role.id))
        return role

    async def ensure_admin_role(self, guild: Guild) -> discord.Role:
        """Create/verify the League Admin role.

        The role carries ``ADMIN_GROUP_PERMISSION``, which is what makes the
        ``/league_admin`` command group visible to the role's holders and
        hidden from everyone else.
        """
        return await self.ensure_role(
            guild, ADMIN_ROLE_NAME, "admin_role_id",
            permissions=ADMIN_GROUP_PERMISSION,
        )

    async def grant_admin(self, guild: Guild, user: discord.User | discord.Member) -> None:
        member = self._as_member(guild, user)
        if member is None or not isinstance(member, Member):
            log.warning(
                "Could not find member %s in guild %s - is the ADMIN_IDS entry a "
                "valid, full Discord user id of someone in the server?",
                getattr(user, "id", user), guild.id,
            )
            return
        role = await self.ensure_admin_role(guild)
        if role not in member.roles:
            await member.add_roles(role, reason="League admin (from .env ADMIN_IDS)")
            log.info("Granted League Admin role to %s (%s)", member.display_name, member.id)

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

    # -- round-boundary role routines -----------------------------------
    # These are attached to the announcement routines in service.py so that
    # role changes happen exactly when the round starts/ends - no timers of
    # their own and no periodic reconciliation.

    async def on_round_start(self, guild: Guild, spec: SeasonSpec, period_index: int) -> None:
        """Sync the seed-not-done role for the round that just started.

        Every registered participant who has not reported the round gains
        the role; anyone holding it who already reported (or is not
        registered anymore) loses it. Runs inline with the round-start
        announcement — on time for a live round, or as a catch-up when the
        bot restores its timers after a restart.
        """
        role = self._find_role(guild, self._config.seed_not_done_role_name)
        if role is None:
            return  # /setup has not run yet
        should_have = self._db.unsubmitted_ids(spec.season_id, period_index)
        changed = 0
        for member in guild.members:
            if not isinstance(member, Member):
                continue
            has_role = role in member.roles
            should = member.id in should_have
            if should and not has_role:
                await member.add_roles(role, reason="Round started")
                changed += 1
            elif has_role and not should:
                await member.remove_roles(role, reason="Already reported this round")
                changed += 1
        if changed:
            log.info("Round start: adjusted seed-not-done role for %d member(s)", changed)

    async def on_round_end(self, guild: Guild, spec: SeasonSpec, period_index: int) -> None:
        """Revoke the seed-not-done role from everyone at round end.

        The round is over, so nobody is "not done" anymore. Runs inline with
        the round-end announcement.
        """
        role = self._find_role(guild, self._config.seed_not_done_role_name)
        if role is None:
            return
        for member in guild.members:
            if isinstance(member, Member) and role in member.roles:
                await member.remove_roles(role, reason="Round ended")

    # -- announcements ---------------------------------------------------
    async def announce_channel(self, guild: Guild) -> Optional[discord.TextChannel]:
        """The channel configured for season/round announcements, if any."""
        channel_id = self.get_announce_channel_id()
        if channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        # The channel may not be in cache yet (e.g. right after startup).
        try:
            fetched = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def announce(self, guild: Guild, message: str) -> bool:
        """Post ``message`` to the announcement channel. Returns success."""
        channel = await self.announce_channel(guild)
        if channel is None:
            log.warning(
                "No announcement channel configured; skipping announcement. "
                "Run /league_admin setup with announce_channel."
            )
            return False
        try:
            await channel.send(message)
        except discord.HTTPException:
            log.exception("Failed to send announcement to #%s", channel.name)
            return False
        return True

    def get_announce_channel_id(self) -> Optional[int]:
        value = self._db.get_setting(ANNOUNCE_CHANNEL_KEY)
        return int(value) if value else None

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

    # -- seed channel permissions ---------------------------------------
    async def apply_announce_channel(self, guild: Guild, channel: discord.TextChannel) -> None:
        """Remember ``channel`` as the season/round announcement channel."""
        self._db.set_setting(ANNOUNCE_CHANNEL_KEY, str(channel.id))
        log.info("Announcement channel set to #%s (id %s)", channel.name, channel.id)

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
