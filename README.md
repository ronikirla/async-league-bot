# Async Speedrun League Bot

A Discord bot for running an asynchronous speedrun league:

- An admin creates a **season** (period length × number of periods, start time).
- Participants **register**, **request the shared RNG seed** for the active period,
  and **submit a time + video link**.
- The bot logs first seed-request times and submissions in a **Google
  spreadsheet** (one tab per season, one row per participant × period).
- The bot manages two roles:
  - **League Participant** — required to request seeds and submit times.
  - **League Seed Not Done** — granted while a participant has NOT submitted the
    active period. This role is denied view access to the seed discussion
    channel, so the seed chat stays spoiler-free for runners who are still playing.

## Project layout

| File | Purpose |
|---|---|
| [`main.py`](main.py) | Bot entry point: connects, syncs commands, starts the role reconciliation loop |
| [`config.py`](config.py) | Loads/validates `.env` configuration |
| [`db.py`](db.py) | SQLite persistence (seasons, participants, period records, settings) |
| [`periods.py`](periods.py) | Season/period math (clock-derived active period) + time parsing |
| [`sheets.py`](sheets.py) | Google Sheets sync via `gspread` (dry-run aware) |
| [`roles.py`](roles.py) | Role creation/granting/revocation + reconciliation |
| [`service.py`](service.py) | High-level operations used by the commands |
| [`cogs/admin.py`](cogs/admin.py) | Admin slash commands |
| [`cogs/participant.py`](cogs/participant.py) | Participant slash commands |
| [`tests/`](tests/) | Unit + end-to-end (dry-run) tests |

## Setup

### 1. Discord application

1. Create an application at <https://discord.com/developers/applications>.
2. On the **Bot** tab:
   - Copy the token → `DISCORD_TOKEN`.
   - Enable **Server Members Intent** (required to read member roles).
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
   `Period | Period Start (UTC) | Period End (UTC) | Participant | Discord ID |
   Seed | Seed Requested At (UTC) | Submitted At (UTC) | Run Time | Video`.
   Rows are pre-created for every registered participant; cells are updated
   in place. All timestamps are UTC ISO 8601.

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

1. Run `/setup` (as an admin) — creates both roles.
2. Create a text channel for seed discussion, then run
   `/setup seed_channel:<the channel>` — applies the permission overrides so
   it is hidden from members with the *League Seed Not Done* role.

## Commands

### Admin (`ADMIN_IDS` only)

| Command | Description |
|---|---|
| `/setup [seed_channel]` | Create/verify both roles; optionally apply the seed-channel permission overrides |
| `/create_season period_length periods start` | Start a new season. `period_length` like `7d`, `12h`, `1d12h`; `start` is ISO 8601 (UTC if no zone) or `now`. Pre-creates sheet rows for all registered participants |
| `/season_info` | Season details, active period, current seed, registered count |
| `/add_participant user` | Manually register a user |
| `/remove_participant user` | Remove a user from the league (roles revoked) |

### Participants

| Command | Description |
|---|---|
| `/register` | Join the league (grants the participant role) |
| `/seed` | Request the current period's seed (ephemeral reply). One seed per period for everyone; the sheet records only the **first** request time |
| `/submit time video` | Submit your run. `time` = `M:SS` or `H:MM:SS` with optional `.mmm`; `video` = http(s) link. Submissions are final per period |

## Behavior notes

- **Seeds:** a single seed per period for the whole league, random in
  `0–9999999999` (inclusive), generated on the first `/seed` of the period.
  Repeated `/seed` calls return the same seed without updating the sheet.
- **Role sync:** the *Seed Not Done* role is granted/removed event-driven
  (register, seed request, submit) and additionally re-synced by a
  background task every `RECONCILE_MINUTES` (default 15 min), so it self-heals
  across restarts and period boundaries.
- **Registration mid-season:** works — the participant's rows are appended to
  the sheet and they receive the already-generated seed on request.
- **Season rollover:** the active period is derived from the UTC clock, so
  periods advance automatically; no restart needed.

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

The test suite covers period math, time parsing, the database layer, and a
full end-to-end dry-run of register → seed → submit.
