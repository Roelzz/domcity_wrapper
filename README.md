# Domcity Planner

Self-hosted Python web app that wraps [members.pushpress.com](https://members.pushpress.com) — view your gym schedule, manage reservations, and automate weekly class sign-ups timed precisely to when the booking window opens.

Built for one gym member ("Dom city" = Utrecht), deployed on a home server via Coolify.

## Features

- **Schedule** — 7-day agenda grid with sticky filter chips for **Location** and **Training**. Multi-select, URL-encoded so views are shareable. Mobile-friendly (columns stack on small screens). Theme follows your OS light/dark preference (Pico.css green palette). Each class card has a **Book** button (HTMX in-place swap), an **⚡ Automate** shortcut, and a "Opens in 3d 14h" countdown when the booking window isn't open yet.
- **Reservations** — your upcoming bookings with location + instructor, one-click cancel.
- **Automation** — recurring rules with cascading dropdowns: pick **Training** → Day dropdown narrows to days the gym runs that training → Time dropdown narrows to actual `HH:MM · Location` combos. When only one option exists at each step, it's **auto-picked**. Per-rule **Pause until** date for vacations. **Fire now** button to test a rule immediately.
- **Smart retry** — distinguishes three failure modes: class-full (poll every 12h/4h/1h/15m as the class approaches), terminal user errors like *cap exceeded* (give up immediately), and transient network errors (30s retries, capped at 5).
- **Auto-refreshing PushPress auth** — the app logs in with email + password on startup, caches the JWT in SQLite, re-logs in via a daily 03:00 cron whenever < 7 days remain. No manual token paste. 401/403 from a GraphQL call triggers an inline refresh + retry.
- **Telegram reminders** — two pings per booked class: 📅 same-day at 08:00 local, ⏰ 30 minutes before class start. Includes location + instructor. Plus success/failure messages on every automation attempt.
- **iCal feed** — `/calendar.ics?token=…` returns your active reservations as a subscribable calendar. Drop the URL into Apple Calendar / Google / Fastmail.
- **/stats** — bookings dashboard: success rate, weekly timeline, per-rule and per-category breakdown.
- **MCP server** — use the gym from **Claude** (Desktop + phone). Read your schedule and book/cancel/automate by chatting, via an `/mcp` endpoint protected by a self-hosted OAuth login. See [MCP server (Claude)](#mcp-server-claude).

## Stack

- Python 3.12, UV, Ruff, Pytest
- FastAPI + Jinja2 + HTMX + Pico.css (no JS build step)
- httpx async client → PushPress GraphQL API (`api.pushpress.com/v2/graph/graphql`)
- APScheduler `AsyncIOScheduler` (in-process cron) · SQLModel + SQLite · loguru
- FastMCP (Streamable HTTP MCP server at `/mcp`) + self-hosted OAuth 2.1 for Claude Desktop/mobile
- 60-second in-memory cache + parallel `asyncio.gather` for sub-20ms warm page loads

## How auth works

The members portal is a Flutter SPA. Login goes through `POST https://api.pushpress.com/v2/auth/login` with `{"username":"...","password":"..."}` and returns `{"accessToken":"<JWT>","refreshToken":"...", ...}`. The access token is a HS256-signed JWT good for 60 days.

This app does the login itself:

1. On startup, `pushpress.ensure_token()` loads the cached JWT from a `TokenCache` row in SQLite.
2. If the cache is missing or expired, it POSTs to `/v2/auth/login` with `PUSHPRESS_EMAIL` + `PUSHPRESS_PASSWORD` and persists the result.
3. A daily APScheduler cron at 03:00 checks the cache; if < 7 days remain, it re-logs in.
4. Any GraphQL call that comes back 401/403 (or with a "token" error) triggers an inline refresh + retry once.
5. If creds become invalid (wrong password / PushPress changed flow), every page renders an error banner so you know to check the logs.

`clientUuid` and `userUuid` are decoded from the JWT. `clientUserUuid` and the active `subscriptionUuid` are fetched once via the `GetProfiles` query and cached in-process.

## Timezone gotcha

PushPress's GraphQL serialises class times like `2026-05-18T16:30:00+00:00` — the `+00:00` suffix is misleading. The digits are the gym's **local** wall-clock, not UTC. `pushpress._parse_dt` strips the suffix and re-attaches the zone configured by `TZ` in `.env` (default `Europe/Amsterdam`).

If you fork this for a gym in a different city, **set `TZ` in `.env`** to that gym's local zone (e.g. `America/Los_Angeles`).

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env:
#   APP_USERNAME       — username to unlock the UI + MCP (default: admin)
#   APP_PASSWORD       — your chosen password to unlock the UI + MCP
#   SECRET_KEY         — python -c "import secrets; print(secrets.token_urlsafe(32))"
#   PUSHPRESS_EMAIL    — your members.pushpress.com email
#   PUSHPRESS_PASSWORD — your members.pushpress.com password
#   TZ                 — gym's local zone, e.g. Europe/Amsterdam
#   MCP_BASE_URL       — public HTTPS origin for the MCP server (prod only)
#   TELEGRAM_BOT_TOKEN — optional, for booking & reminder notifications
#   TELEGRAM_CHAT_ID   — optional

uv run uvicorn main:app --port 2009
```

Open <http://localhost:2009> → enter `APP_PASSWORD` → schedule loads.

> `FERNET_KEY` is in `.env.example` for backwards compatibility but the app no longer uses it (the JWT lives in SQLite plain — same trust boundary as `.env`). Safe to leave blank.

### Telegram bot setup (optional)

1. In Telegram, search `@BotFather`, send `/newbot`, follow the prompts. Save the token.
2. Search for your new bot, click **Start**, send any message.
3. `curl https://api.telegram.org/bot<TOKEN>/getUpdates` — find `"chat":{"id":<number>}` in the JSON. That number is `TELEGRAM_CHAT_ID`.
4. Paste both into `.env`, restart the app.

## MCP server (Claude)

The app exposes itself as an **MCP server** at `/mcp`, so Claude can read your schedule and book classes for you — both from **Claude Desktop** on your laptop and from **Claude on your phone** (the iOS/Android app and claude.ai web). One deployed endpoint serves both.

### Tools

Read: `get_schedule`, `get_reservations`, `get_stats`, `get_tenant_info`, `list_automation_rules`.
Write (real side effects on your gym account): `book_class`, `cancel_reservation`, `create_automation_rule`, `toggle_automation_rule`, `pause_automation_rule`, `delete_automation_rule`, `fire_automation_rule`.

Because MCP runs **in the same process** as the web app, it shares the live APScheduler, SQLite DB, and cached PushPress token — so a rule created by Claude is armed by the scheduler exactly like one created in the web UI.

### Auth

The MCP endpoint is protected by a **self-hosted OAuth 2.1 server** built into the app (Claude's mobile/web custom connector rejects static bearer tokens — OAuth is mandatory). The OAuth login screen validates the **same `APP_USERNAME` + `APP_PASSWORD`** from `.env` that gates the web UI. No third-party IdP, no extra accounts. Claude registers itself automatically (Dynamic Client Registration); you just log in once.

> OAuth client/token state is in-memory — on a server restart Claude transparently re-registers and asks you to log in again. No DB schema change, no infra change.

### Connect from Claude (Desktop + phone)

1. Deploy behind HTTPS (Coolify gives you TLS) and set `MCP_BASE_URL` to your **exact public origin**, e.g. `https://domcity.example.com`. This must match the domain Claude connects to or OAuth discovery/redirect validation fails.
2. In Claude → **Settings → Connectors → Add custom connector**.
3. URL: `https://<your-domain>/mcp`
4. Claude opens the login screen → enter your `APP_USERNAME` + `APP_PASSWORD` → done. The same connector now works on the phone app and claude.ai.

### Local dev

```bash
uv run uvicorn main:app --port 2009
```

MCP is then at `http://localhost:2009/mcp`. Claude Desktop can use a local HTTP MCP endpoint directly; the phone/web connector needs the public HTTPS URL above. `MCP_BASE_URL` defaults to `http://localhost:2009` for local use.

## Tests

```bash
uv run ruff check .
uv run pytest
```

Tests cover title parsing, scheduler timing + matching, route auth + filters, GraphQL client (mocked via respx), login flow, the MCP tool wrappers (via FastMCP's in-memory client), and the self-hosted OAuth credential gate + PKCE code round-trip.



## Deploy (Coolify + Nixpacks)

1. Push this repo to GitHub.
2. New Coolify app → Source = your repo → Build pack = **Nixpacks** (auto-detects via `nixpacks.toml`). `uv.lock` is committed so `uv sync --frozen` works at build time.
3. **Environment Variables**: copy from `.env.example`, fill in real values.
4. **Persistent Storage** → **+ Add** → **Volume Mount**:
   - Name: `domcity-data`
   - Source path: *(leave empty)*
   - Destination path: `/app/data`
5. Expose port `2009`.
6. Point your proxy/domain at the Coolify app.

Without step 4, your `TokenCache` + automation rules + booking history get wiped on every redeploy. With it, they persist in a Docker named volume.

## Project layout

```
Domcity/
├── main.py                                # FastAPI app, routes, lifespan, /mcp mount
├── mcp_server.py                          # FastMCP server + tools (schedule, book, automation)
├── mcp_oauth.py                           # Self-hosted OAuth 2.1 provider (.env credential gate)
├── pushpress.py                           # GraphQL client + auto-refresh
├── scheduler.py                           # APScheduler + booking + reminders + token refresh
├── auth.py                                # App-password gate, signed cookie middleware
├── models.py                              # SQLModel: AutomationRule, BookingAttempt, TokenCache
├── notify.py                              # Telegram notifier (httpx → Bot API)
├── settings.py                            # pydantic-settings
├── crypto.py                              # Fernet wrapper (vestigial; unused)
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── schedule.html                      # 7-day grid + filter chips + countdown
│   ├── reservations.html
│   ├── automation.html                    # form with cascading dropdowns
│   ├── stats.html                         # /stats dashboard
│   ├── _automation_day_time.html          # HTMX partial: cascading Day + Time-slot
│   └── _time_slot_select.html             # legacy partial (kept for compat shim)
├── static/app.css                         # Chip styles, day-grid, responsive, dark mode
├── tests/                                 # title parsing, scheduler, routes, MCP tools, OAuth
├── docs/                                  # gitignored HARs (endpoint reference)
├── LICENSE                                # MIT
├── nixpacks.toml
├── Procfile
├── pyproject.toml
├── uv.lock                                # committed for reproducible Nixpacks builds
└── .env.example
```

## How the automation engine works

Each rule stores `name`, `location`, `class_category`, `day_of_week`, `time_of_day`, `enabled`, `paused_until`. There's no per-rule lead time — the scheduler reads each matched class's `registrationStartOffset` (in minutes, negative — e.g. -20160 = 14 days) to compute exactly when its booking window opens. Different class types at the same gym can have different windows; the app respects them all.

On startup (and after every rule add / toggle / successful booking) the scheduler:

1. Fetches the next 14 days of classes (parallel per-day queries, cached 60s).
2. For each enabled rule, finds the next class matching `location + category + day_of_week + time_of_day` (skipping anything before `paused_until`).
3. Computes `fire_at = class_start + registrationStartOffset` — the exact tick the window opens.
4. If `fire_at` is in the past (window already open at rule-create time), fires in 2 seconds.
5. Schedules a one-shot `AsyncIOScheduler` job at `fire_at`.

When the job runs:

1. Refreshes the matched class (state may have changed in the last few days).
2. If `spots_available == 0` at window-open → skip the doomed POST, switch to poll mode.
3. Otherwise, POST `createReservation`.
4. **Success** → log `BookingAttempt(status=success)`, Telegram ✅, schedule reminders, re-schedule rule for the following week's class.
5. **Class full** error → switch to poll mode.
6. **Terminal user error** (cap exceeded / already reserved / membership / etc.) → Telegram ❌, give up, re-arm for next week.
7. **Transient** (network / 5xx) → retry every 30s, up to 5 attempts.

### Poll mode (when class is full)

When a class is full, retrying every 30s is pointless — spots only open up when someone cancels. The scheduler switches to an adaptive poll:

| Time until class | Next poll in |
|---|---|
| > 48h | 12h |
| 24h – 48h | 4h |
| 6h – 24h | 1h |
| 1h – 6h | 15m |
| ≤ 1h | stop |

Each poll re-fetches the class and tries again if `spots_available > 0`. If the class starts before a spot opens, give up and advance to next week.

### Telegram reminders

For every active reservation the scheduler keeps two one-shot jobs scheduled:

- 📅 at 08:00 local on the class day (if the class is later than 08:00)
- ⏰ 30 minutes before class start

Cancelled bookings: the orphaned job still fires at its time but verifies the reservation is still active and silently skips otherwise. New bookings get reminders immediately (no waiting for the 15-minute scan).

## License

MIT
