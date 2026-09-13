# Async Speedrun League Bot

A Discord bot for running an asynchronous speedrun league:

- An admin creates a **season** (round length × number of rounds, start time).
- Participants **register**, **request the shared RNG seed** for the active round,
  and **submit a time + video link**.
- The bot logs first seed-request times and submissions in a **Google
  spreadsheet** (one tab per season, one row per participant × round).
- The bot manages two roles:
  - **League Participant** — required to request seeds and submit times.
  - **League Seed Not Done** — granted while a participant has NOT submitted the
    active round. This role is denied view access to the seed discussion
    channel, so the seed chat stays spoiler-free for runners who are still playing.

## Project layout

| File | Purpose |
|---|---|
| [`main.py`](main.py) | Bot entry point: connects, syncs commands, runs the reconciliation loop (roles + season-end cleanup) |
| [`config.py`](config.py) | Loads/validates `.env` configuration |
| [`db.py`](db.py) | SQLite persistence (seasons, participants, period records, settings) |
| [`periods.py`](periods.py) | Season/round math (clock-derived active round) + time parsing |
| [`sheets.py`](sheets.py) | Google Sheets sync via `gspread` (dry-run aware, idempotent writes) |
| [`roles.py`](roles.py) | Role creation/granting/revocation + reconciliation |
| [`service.py`](service.py) | High-level operations used by the commands |
| [`cogs/league.py`](cogs/league.py) | All slash commands (single `/league` group) |
| [`tests/`](tests/) | Unit + end-to-end (dry-run) tests |

## Setup

### 1. Discord application

1. Create an application at <https://discord.com/developers/applications>.
2. On the **Bot** tab:
   - Copy the token → `DISCORD_TOKEN`.
   - Under **Privileged Gateway Intents**, enable **SERVER MEMBERS INTENT**
     (required to read/manage member roles).
3. Invite the bot with scopes `bot` + `applications.commands` and these
   permissions: **Manage Roles**, **Manage Channels**, **View Channels**,
   **Send Messages**, **Use Slash Commands**.
   (Role management requires the bot's top role to be above the league roles
   in the role list.)
4. In the server: enable Developer Mode in Discord settings, right-click the
   server → *Copy Server ID* → `GUILD_ID`.
5. Put your user ID(s) in `ADMIN_IDS` (comma-separated) so you can use the
   admin commands.

### 2. Google spreadsheet

1. Create a Google Spreadsheet; copy the spreadsheet ID from its URL
   (`.../d/<SPREADSHEET_ID>/...`) → `GOOGLE_SPREADSHEET_ID`.
2. Create a Google Cloud service account:
   - <https://console.cloud.google.com/> → create/select a project →
     **IAM & Admin → Service Accounts → Create Service Account** →
     **Keys → Add Key → JSON** → download the file.
3. Place the JSON file at `credentials/service_account.json`
   (path configurable via `GOOGLE_SERVICE_ACCOUNT_FILE`).
4. In the spreadsheet, **Share** it with the service account's email
   (the `client_email` in the JSON) as an **Editor**.
5. The bot creates one tab per season (`S1`, `S2`, …) with headers:
   `Round | Round Start (UTC) | Round End (UTC) | Participant | Discord ID |
   Seed | Seed Requested At (UTC) | Submitted At (UTC) | Run Time | Video`.
   Rows are pre-created for every registered participant; cells are updated
   in place and never clobbered. All timestamps are UTC ISO 8601.

### 3. Configure & run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the values
.venv/bin/python main.py
```

To test the bot **without** Google credentials, set `DRY_RUN=1` in `.env`
(sheet writes are logged to `dry_run_rows.json` instead of being sent).

### 4. First-time server setup

1. Restart the bot — it auto-creates the **League Admin** role (with the
   Manage Roles permission) and grants it to every user in `ADMIN_IDS`.
2. Run `/league_admin setup` (as an admin) — creates the participant and
   seed-not-done roles.
3. Create a text channel for seed discussion, then run
   `/league_admin setup seed_channel:<the channel>` — applies the permission
   overrides so it is hidden from members with the *League Seed Not Done* role.

## Commands

Two groups:

- **`/league`** — participant commands, visible to everyone.
- **`/league_admin`** — admin commands, **hidden from members without the
  Manage Roles permission** (the gate can be switched to Administrator by
  changing `ADMIN_GROUP_PERMISSION` in [`roles.py`](roles.py) if the bot
  itself has Administrator). The bot grants that permission to the
  *League Admin* role (given to the users in `ADMIN_IDS` at startup), so the
  group only appears for league admins.

### Admin (`/league_admin`, admins only)

| Command | Description |
|---|---|
| `/league_admin setup [seed_channel]` | Create/verify the league roles; optionally apply the seed-channel permission overrides |
| `/league_admin create_season round_length rounds start` | Start a new season. `round_length` like `7d`, `12h`, `1d12h`; `start` is ISO 8601 (UTC if no zone) or `now`. Pre-creates sheet rows for all registered participants |
| `/league_admin season_info` | Season details, active round, current seed, registered count. Also clears registrations from any ended season |
| `/league_admin add_participant user` | Manually register a user (works even during an active round) |
| `/league_admin remove_participant user` | Remove a user from the league (roles revoked) |

### Participants

| Command | Description |
|---|---|
| `/league register` | Join the league. Only allowed while no round is active (before the season starts, or after it ends). Grants the participant role |
| `/league unregister` | Leave the league. Same timing restriction as registration |
| `/league seed` | Request the current round's seed (ephemeral reply). One seed per round for everyone; the sheet records only the **first** request time |
| `/league submit time video` | Submit your run. `time` = `M:SS` or `H:MM:SS` with optional `.mmm`; `video` = http(s) link. Submissions are final per round |

## Behavior notes

- **Seeds:** a single seed per round for the whole league, random in
  `0–9999999999` (inclusive), generated on the first `/league seed` of the round.
  Repeated requests return the same seed without updating the sheet.
- **Registration lifecycle:** participants register before a season starts
  (or after one ends). When a season ends, **all registrations are cleared**
  automatically (by the background loop and by `/league_admin season_info`), and
  the participant/seed-not-done roles are revoked.
- **Role sync:** the *Seed Not Done* role is granted/removed event-driven
  (register, seed request, submit) and additionally re-synced by a background
  task every `RECONCILE_MINUTES` (default 15 min), so it self-heals across
  restarts and round boundaries.
- **Sheet writes are idempotent:** a cell is only filled when empty, and row
  pre-creation merges with existing values, so re-runs or stale reads can
  never overwrite a seed, timestamp, or submission. If a write to the sheet
  fails, the next `/league seed` or `/league submit` re-syncs the
  database-stored value into the empty cell.
- **Season rollover:** the active round is derived from the UTC clock, so
  rounds advance automatically; no restart needed.

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

The test suite covers round math, time parsing, the database layer, and a
full end-to-end dry-run of the registration → seed → submit lifecycle.
