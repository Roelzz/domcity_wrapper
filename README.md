# Domcity Planner

Self-hosted Python web app that wraps the [members.pushpress.com](https://members.pushpress.com) members portal — view your gym schedule, see your reservations, and automate weekly class sign-ups timed precisely to when the booking window opens.

Built for one gym member, deployed on a home server via Coolify.

## Features

- **Schedule** — browse upcoming classes (week navigation, mobile + desktop)
- **Reservations** — view bookings, cancel
- **Automation** — recurring rules: e.g. "every Wednesday's 09:00 Classic CrossFit, auto-book the moment the booking window opens 14 days ahead". Retries on failure. Telegram notifications.

## Stack

- Python 3.12, UV, Ruff, Pytest
- FastAPI + Jinja2 + HTMX + Pico.css
- httpx async client → PushPress GraphQL API
- APScheduler (in-process cron) · SQLModel + SQLite · Fernet · loguru

## How auth works

The members portal is a Flutter SPA backed by GraphQL at `api.pushpress.com/v2/graph/graphql`. Auth is a bearer JWT (HS256, server-signed, ~60-day lifetime). There's no programmatic login — PushPress mints the token after browser form login. To use this app:

1. Log into <https://members.pushpress.com> in Chrome/Edge.
2. Open DevTools → Network → click any `graphql` request.
3. In the Headers tab, copy the value of `Authorization` (the long token after `Bearer `).
4. Paste it into `.env` as `PUSHPRESS_TOKEN=` (without the `Bearer ` prefix).
5. The app warns you 7 days before expiry (via Telegram + UI banner). Repeat steps 1-4 when prompted.

`clientUuid` and `userUuid` are decoded from the JWT itself. `clientUserUuid` and `subscriptionUuid` are auto-discovered via a `GetProfiles` query at first request.

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env:
#   APP_PASSWORD        — your chosen password to unlock the UI
#   SECRET_KEY          — python -c "import secrets; print(secrets.token_urlsafe(32))"
#   FERNET_KEY          — python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   PUSHPRESS_TOKEN     — see "How auth works" above
#   TELEGRAM_BOT_TOKEN  — optional, for booking-result notifications
#   TELEGRAM_CHAT_ID    — optional

uv run uvicorn main:app --port 2009
```

Open <http://localhost:2009> → enter `APP_PASSWORD` → schedule.

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
6. Mount a persistent volume at `./data/` so the SQLite DB + automation rules survive redeploys.

## Project layout

```
Domcity/
├── main.py              # FastAPI app + routes + lifespan
├── pushpress.py         # GraphQL client (httpx + JWT)
├── scheduler.py         # APScheduler + booking_window_job
├── auth.py              # Password gate
├── models.py            # SQLModel tables
├── crypto.py            # Fernet wrapper
├── notify.py            # Telegram notifier
├── settings.py          # pydantic-settings
├── templates/           # Jinja2 (base, login, schedule, reservations, automation)
├── static/app.css
├── tests/
├── docs/endpoints.md    # (gitignored) your captured endpoint reference
├── nixpacks.toml
├── Procfile
├── pyproject.toml
└── .env.example
```

## How the automation works

Each rule encodes "class name substring", "day of week", "time of day", and "lead time hours" — how far ahead the booking window opens at your gym. PushPress sets this per class type via `registrationStartOffset` (in minutes). This CrossFit gym uses **14 days = 336h** for Classic CrossFit, the default in `.env`.

On startup, the scheduler computes the next moment the window opens for each enabled rule and schedules a one-shot APScheduler job. The job:

1. Fetches the schedule for that day
2. Finds the matching class (substring match + within 30 min of expected time)
3. POSTs `CreateReservation`
4. On failure, retries every 30s up to 10 times
5. Sends a Telegram message with the result
6. Records a `BookingAttempt` row
7. Schedules itself for the same time next week

Timing is accurate to ~1s — sufficient for a public booking window race.

## License

MIT
