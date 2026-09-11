"""Configuration loading from a .env file.

Run the bot from the project root with a filled-in ``.env`` file
(see ``.env.example``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at import time
    pass

DEFAULT_PARTICIPANT_ROLE = "League Participant"
DEFAULT_SEED_NOT_DONE_ROLE = "League Seed Not Done"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}. See .env.example.")
    return value


def _parse_ids(raw: str, name: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise ConfigError(f"{name} contains a non-numeric id: {part!r}")
    if not ids:
        raise ConfigError(f"{name} must contain at least one comma-separated user id")
    return ids


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    admin_ids: tuple[int, ...]
    spreadsheet_id: str
    service_account_file: str
    participant_role_name: str
    seed_not_done_role_name: str
    dry_run: bool
    reconcile_minutes: int

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_config() -> Config:
    """Load and validate configuration from the environment."""
    token = _require("DISCORD_TOKEN")
    try:
        guild_id = int(_require("GUILD_ID"))
    except ValueError as exc:
        raise ConfigError("GUILD_ID must be a numeric Discord guild id") from exc

    try:
        reconcile_minutes = int(os.getenv("RECONCILE_MINUTES", "15") or "15")
    except ValueError as exc:
        raise ConfigError("RECONCILE_MINUTES must be an integer") from exc
    if reconcile_minutes <= 0:
        raise ConfigError("RECONCILE_MINUTES must be positive")

    dry_run = os.getenv("DRY_RUN", "").strip() == "1"

    # Google credentials are only required when not in dry-run mode.
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not dry_run:
        if not spreadsheet_id:
            raise ConfigError(
                "GOOGLE_SPREADSHEET_ID is required unless DRY_RUN=1. "
                "It is the long string in the sheet URL after /d/."
            )
        if not service_account_file:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_FILE is required unless DRY_RUN=1.")

    return Config(
        token=token,
        guild_id=guild_id,
        admin_ids=tuple(_parse_ids(os.getenv("ADMIN_IDS", ""), "ADMIN_IDS")),
        spreadsheet_id=spreadsheet_id,
        service_account_file=service_account_file,
        participant_role_name=os.getenv("PARTICIPANT_ROLE_NAME", DEFAULT_PARTICIPANT_ROLE).strip()
        or DEFAULT_PARTICIPANT_ROLE,
        seed_not_done_role_name=os.getenv("SEED_NOT_DONE_ROLE_NAME", DEFAULT_SEED_NOT_DONE_ROLE).strip()
        or DEFAULT_SEED_NOT_DONE_ROLE,
        dry_run=dry_run,
        reconcile_minutes=reconcile_minutes,
    )
