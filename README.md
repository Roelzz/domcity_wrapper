# Domcity Planner

Self-hosted Python web app that wraps [members.pushpress.com](https://members.pushpress.com) — view your gym schedule, manage reservations, and automate weekly class sign-ups timed precisely to when the booking window opens.

Built for one gym member ("Dom city" = Utrecht), deployed on a home server via Coolify.

## Features

- **Schedule** — 7-day agenda grid with sticky filter chips for **Location** and **Training**. Multi-select, URL-encoded so views are shareable. Mobile-friendly (columns stack on small screens). Each class card has a **Book** button (HTMX in-place swap) and an **⚡ Automate** shortcut.
- **Reservations** — your upcoming bookings, one-click cancel.
- **Automation** — recurring rules with cascading dropdowns: pick **Training** → Day dropdown narrows to days the gym runs that training → Time dropdown narrows to actual `HH:MM · Location` combos. When only one option exists at each step, it's **auto-picked**. Rule fires the moment its booking window opens, retries on transient failure, fails fast on terminal errors (quota exceeded, etc.), and re-arms for the next week.
- **Auto-refreshing PushPress auth** — the app logs in with email + password on startup, caches the JWT in SQLite, and re-logs-in via a daily 03:00 cron whenever the cached token has < 7 days left. No more manual token paste. On any 401/403 from a GraphQL call it refreshes inline and retries.
- **Telegram notifications** on every booking success, every retry, every terminal failure, every token refresh.

## Stack

- Python 3.12, UV, Ruff, Pytest
- FastAPI + Jinja2 + HTMX + Pico.css (no JS build step)
- httpx async client → PushPress GraphQL API (`api.pushpress.com/v2/graph/graphql`)
- APScheduler `AsyncIOScheduler` (in-process cron) · SQLModel + SQLite · loguru
- 60-second in-memory cache + parallel `asyncio.gather` for sub-20ms warm page loads

## How auth works

The members portal is a Flutter SPA. Login goes through `POST https://api.pushpress.com/v2/auth/login` with `{"username":"...","password":"..."}` and returns `{"accessToken":"<JWT>","refreshToken":"...", ...}`. The access token is a HS256-signed JWT good for 60 days.

This app does the login itself:

1. On startup, `pushpress.ensure_token()` loads the cached JWT from a `TokenCache` row in SQLite.
2. If the cache is missing or expired, it POSTs to `/v2/auth/login` with `PUSHPRESS_EMAIL` + `PUSHPRESS_PASSWORD` and persists the result.
3. A daily APScheduler cron at 03:00 checks the cache; if < 7 days remain, it re-logs in.
4. Any GraphQL call that comes back 401/403 (or with a "token" error) triggers an inline refresh + retry once.

`clientUuid` and `userUuid` are decoded from the JWT. `clientUserUuid` and the active `subscriptionUuid` are fetched once via the `GetProfiles` query and cached in-process.

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env:
#   APP_PASSWORD       — your chosen password to unlock the UI
#   SECRET_KEY         — python -c "import secrets; print(secrets.token_urlsafe(32))"
#   FERNET_KEY         — python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   PUSHPRESS_EMAIL    — your members.pushpress.com email
#   PUSHPRESS_PASSWORD — your members.pushpress.com password
#   TELEGRAM_BOT_TOKEN — optional, for booking-result notifications (see below)
#   TELEGRAM_CHAT_ID   — optional

uv run uvicorn main:app --port 2009
```

Open <http://localhost:2009> → enter `APP_PASSWORD` → schedule loads.

### Telegram bot setup (optional)

1. In Telegram, search `@BotFather`, send `/newbot`, follow the prompts. Save the token (`8631...:AAEd...`).
2. Search for your new bot, click **Start**, send any message.
3. `curl https://api.telegram.org/bot<TOKEN>/getUpdates` — find `"chat":{"id":<number>}` in the JSON. That number is `TELEGRAM_CHAT_ID`.
4. Paste both into `.env`, restart the app.

## Tests

```bash
uv run ruff check .
uv run pytest
```

30 tests cover: title parsing, scheduler timing logic, route auth, GraphQL client (mocked via respx), login flow.

## Deploy (Coolify + Nixpacks)

1. Push this repo to GitHub.
2. New Coolify app → Source = your repo → Build pack = **Nixpacks** (auto-detects via `nixpacks.toml`).
3. Environment variables: copy from `.env.example`, fill in real values.
4. Expose port `2009`.
5. Mount a persistent volume at `./data/` so SQLite (token cache, automation rules, booking attempts) survives redeploys.
6. Point your proxy/domain at the Coolify app.

## Project layout

```
Domcity/
├── main.py                                # FastAPI app, routes, lifespan
├── pushpress.py                           # GraphQL client + auto-refresh
├── scheduler.py                           # APScheduler + booking + token-refresh jobs
├── auth.py                                # App-password gate, signed cookie middleware
├── models.py                              # SQLModel: AutomationRule, BookingAttempt, TokenCache
├── crypto.py                              # Fernet wrapper (unused since DB stores plain JWT)
├── notify.py                              # Telegram notifier (httpx → Bot API)
├── settings.py                            # pydantic-settings
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── schedule.html                      # 7-day grid + filter chips
│   ├── reservations.html
│   ├── automation.html                    # form with cascading dropdowns
│   └── _automation_day_time.html          # HTMX partial: cascading Day + Time-slot
├── static/app.css                         # Chip styles, day-grid, responsive
├── tests/                                 # 30 tests, all green
├── nixpacks.toml
├── Procfile
├── pyproject.toml
└── .env.example
```

## How the automation engine works

Each rule stores `name`, `location`, `class_category`, `day_of_week`, `time_of_day`, `enabled`. There's no per-rule "lead time" anymore — the scheduler reads each matched class's `registrationStartOffset` (in minutes, negative — e.g. -20160 = 14 days) to compute exactly when its booking window opens. Different class types at the same gym can have different windows; the app respects them all.

On startup (and after every rule add / toggle / successful booking) the scheduler:

1. Fetches the next 14 days of classes (parallel per-day queries, cached 60s).
2. For each enabled rule, finds the next class matching `location + category + day_of_week + time_of_day`.
3. Computes `fire_at = class_start + registrationStartOffset` — the exact tick the window opens.
4. If `fire_at` is in the past (window already open at rule-create time), fires in 2 seconds.
5. Schedules a one-shot `AsyncIOScheduler` job at `fire_at`.

When the job runs:

1. Refreshes the matched class (booking-state may have changed in the last few days).
2. POSTs `createReservation` mutation.
3. Success → log `BookingAttempt(status=success)`, Telegram ping, re-schedule for the following week's class.
4. Failure → check if the error is **terminal** (substrings like `"exceeded"`, `"already"`, `"membership"`, `"not allowed"` — quota/permission problems that retrying won't fix). Terminal → log + Telegram + re-arm for next week, no retry loop. Otherwise → schedule a retry job 30s later (up to 10 retries).

Timing is accurate to ~1s, which is plenty for a typical public class-booking window race.

## License

MIT
