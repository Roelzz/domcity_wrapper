# Domcity Planner

Self-hosted Python web app that wraps the [members.pushpress.com](https://members.pushpress.com) members portal — view your gym schedule, see your reservations, and automate weekly class sign-ups timed precisely to when the booking window opens.

Built for one gym member, deployed on a home server via Coolify.

## Features

- **Schedule** — browse upcoming classes (week navigation, mobile + desktop)
- **Reservations** — view bookings, cancel
- **Automation** — recurring rules: "every Monday's 17:00 class, auto-book the moment the booking window opens 24h before". Retries on failure. Telegram notifications.

## Stack

- Python 3.12, UV, Ruff, Pytest
- FastAPI + Jinja2 + HTMX + Pico.css
- httpx (PushPress client) · APScheduler (cron) · SQLModel + SQLite · Fernet (creds encryption) · loguru

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env, especially:
#  - APP_PASSWORD          (chosen by you)
#  - SECRET_KEY            (python -c "import secrets; print(secrets.token_urlsafe(32))")
#  - FERNET_KEY            (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
#  - PUSHPRESS_BASE_URL    (your gym's subdomain, e.g. https://yourgym.members.pushpress.com)
#  - PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD
#  - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional)

uv run uvicorn main:app --port 2009
```

Open <http://localhost:2009> → password prompt → schedule.

## ⚠️ Required before live use: endpoint discovery

PushPress publishes no member API. `pushpress.py` ships with **speculative endpoint paths and JSON shapes** that almost certainly need to be adjusted to your gym's actual portal.

1. Open Chrome on `members.pushpress.com` (your gym subdomain), open DevTools → Network → enable "Preserve log".
2. Log in, view schedule, view reservations, book a class, cancel one.
3. Save all network calls as HAR (`docs/session.har`).
4. Fill in `docs/endpoints.md` (template: `docs/endpoints.md.example`).
5. Update the `ENDPOINTS` block and the `_parse_*` functions in `pushpress.py`.
6. Re-run `uv run pytest` — fixtures should still pass.
7. Smoke-test against real PushPress.

## Tests

```bash
uv run ruff check .
uv run pytest
```

## Deploy (Coolify + Nixpacks)

1. Push this repo to GitHub.
2. New Coolify app → Source = your repo → Build pack = **Nixpacks** (auto-detects via `nixpacks.toml`).
3. Environment variables: copy from `.env.example`, fill in real values.
4. Expose port `2009`.
5. Point your proxy/domain at the Coolify app.

Coolify will rebuild on every push. The SQLite DB lives in `./data/` — mount that as a persistent volume in Coolify so rules and attempt history survive redeploys.

## Project layout

```
Domcity/
├── main.py              # FastAPI app + routes + lifespan
├── pushpress.py         # PushPress async client (HTTP)
├── scheduler.py         # APScheduler + booking_window_job
├── auth.py              # Password gate
├── models.py            # SQLModel tables
├── crypto.py            # Fernet wrapper
├── notify.py            # Telegram notifier
├── settings.py          # pydantic-settings
├── templates/           # Jinja2 (base, login, schedule, reservations, automation)
├── static/app.css
├── tests/
├── docs/endpoints.md    # (gitignored) your real endpoint notes
├── nixpacks.toml
├── Procfile
├── pyproject.toml
└── .env.example
```

## How the automation works

Each rule encodes "class name pattern", "day of week", "time of day", and "lead time hours" (how far ahead the booking window opens — PushPress default is 24h, priority members 48h).

On startup, the scheduler computes the next moment the window opens for each enabled rule and schedules a one-shot job. The job:

1. Logs into PushPress
2. Finds the matching class slot in the schedule
3. POSTs the booking
4. On failure, retries every 30s up to 10 times
5. Sends a Telegram message with the result
6. Records a `BookingAttempt` row
7. Schedules itself for the same time next week

Timing is accurate to APScheduler tolerance (~1s) — usually enough unless your gym holds millisecond-level races.

## License

MIT
