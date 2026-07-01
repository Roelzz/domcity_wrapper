"""MCP server for Domcity Planner.

Exposes the gym-booking app as Model Context Protocol tools so Claude (desktop
and mobile/web) can read the schedule, book/cancel classes, and manage booking
automation rules. Mounted at ``/mcp`` inside the existing FastAPI app so every
tool shares the live APScheduler, SQLite DB, and cached PushPress token.

Auth is handled by ``DomcityOAuthProvider`` (self-hosted OAuth 2.1). The
``/mcp/login`` custom route is the single credential entry point — it is exempt
from FastMCP's auth middleware and validates the ``.env`` username/password.
"""

from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from html import escape

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from loguru import logger
from sqlmodel import Session as DbSession
from sqlmodel import select
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import pushpress
import scheduler
from mcp_oauth import DomcityOAuthProvider
from models import AutomationRule, BookingAttempt, engine
from settings import settings

DAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# --- OAuth provider + MCP server singletons -------------------------------- #
# base_url carries the /mcp mount prefix; base_url + mcp_path = the externally
# reachable MCP URL. mcp_app is built with path="/" and mounted at "/mcp" in
# main.py, so the final endpoint is <mcp_base_url>/mcp.
provider = DomcityOAuthProvider(
    base_url=settings.mcp_base_url.rstrip("/") + "/mcp",
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
)

mcp = FastMCP("Domcity Planner", auth=provider)


# --- Read tools ------------------------------------------------------------ #
@mcp.tool
async def get_schedule(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """List CrossFit classes between two dates (inclusive, ISO ``YYYY-MM-DD``).

    Defaults to today through 7 days ahead. Each class includes ``id`` (the
    calendar_item_uuid you pass to ``book_class``), name, location, category,
    start/end times, instructor, spots available/total, and whether you have
    already booked it.
    """
    today = datetime.now(scheduler.tz()).date()
    try:
        start = date.fromisoformat(start_date) if start_date else today
        end = date.fromisoformat(end_date) if end_date else today + timedelta(days=7)
    except ValueError as e:
        raise ToolError(f"bad date, expected YYYY-MM-DD: {e}") from e
    if end < start:
        raise ToolError("end_date must be on or after start_date")
    slots = await pushpress.list_schedule(start, end)
    return [s.model_dump(mode="json") for s in slots]


@mcp.tool
async def get_reservations() -> list[dict]:
    """List your upcoming booked classes.

    Each reservation includes ``id`` (pass to ``cancel_reservation``), the class
    name, location, start/end times, instructor, and whether it is cancellable.
    """
    res = await pushpress.list_reservations()
    return [r.model_dump(mode="json") for r in res]


@mcp.tool
async def get_tenant_info() -> dict:
    """Return your PushPress account context: client, user, and active
    subscription identifiers."""
    tenant = await pushpress.get_tenant()
    return asdict(tenant)


@mcp.tool
async def get_session_credits() -> list[dict]:
    """Show your PushPress session/credit balance for the current billing period.

    PushPress caps how many sessions each subscription may book per period. For
    every subscription this returns the period ``limit``, sessions already
    committed (``reservations`` = upcoming bookings, ``checkins`` = attended),
    ``used`` (their sum), ``remaining`` credits, and the period window
    (``period_start``/``period_end``). ``remaining`` is ``null`` for unlimited
    plans. Use this to see whether you are out of sessions *before* a booking
    fails with "you are out of sessions for this class"."""
    usage = await pushpress.list_subscription_usage()
    return [u.model_dump(mode="json") for u in usage]


@mcp.tool
def list_automation_rules() -> list[dict]:
    """List all booking automation rules and their current state (enabled,
    paused-until, backup chain)."""
    with DbSession(engine) as db:
        rules = db.exec(select(AutomationRule)).all()
        return [_rule_to_dict(r) for r in rules]


@mcp.tool
def get_stats() -> dict:
    """Booking-automation statistics: overall success/failure/polling/retry
    counts, success rate, total and enabled rule counts, an 8-week success
    timeline, per-rule stats, and successful bookings per class category."""
    with DbSession(engine) as db:
        attempts = db.exec(select(BookingAttempt)).all()
        rules = db.exec(select(AutomationRule)).all()

        n_success = sum(1 for a in attempts if a.status == "success")
        n_failure = sum(1 for a in attempts if a.status == "failure")
        n_polling = sum(1 for a in attempts if a.status == "polling")
        n_retry = sum(1 for a in attempts if a.status == "retry")
        decisive = n_success + n_failure
        success_rate = (n_success / decisive * 100) if decisive else 0.0
        enabled_count = sum(1 for r in rules if r.enabled)

        now_local = datetime.now(scheduler.tz())
        this_monday = now_local.date() - timedelta(days=now_local.weekday())
        weeks = []
        for i in range(8):
            wk_start = this_monday - timedelta(weeks=7 - i)
            wk_end = wk_start + timedelta(days=7)
            count = sum(
                1
                for a in attempts
                if a.status == "success" and wk_start <= a.fired_at.date() < wk_end
            )
            weeks.append({"week_start": wk_start.isoformat(), "successes": count})

        rule_stats = []
        for r in rules:
            rule_attempts = [a for a in attempts if a.rule_id == r.id]
            succ = sum(1 for a in rule_attempts if a.status == "success")
            fail = sum(1 for a in rule_attempts if a.status == "failure")
            last = max((a.fired_at for a in rule_attempts), default=None)
            rule_stats.append(
                {
                    "rule_id": r.id,
                    "name": r.name,
                    "category": r.class_category,
                    "success": succ,
                    "failure": fail,
                    "last_attempt": last.isoformat() if last else None,
                }
            )
        rule_stats.sort(key=lambda x: -x["success"])

        by_category: dict[str, int] = {}
        for a in attempts:
            if a.status != "success":
                continue
            rule = next((r for r in rules if r.id == a.rule_id), None)
            if rule:
                by_category[rule.class_category] = by_category.get(rule.class_category, 0) + 1

    return {
        "n_success": n_success,
        "n_failure": n_failure,
        "n_polling": n_polling,
        "n_retry": n_retry,
        "success_rate": round(success_rate, 1),
        "total_rules": len(rules),
        "enabled_count": enabled_count,
        "weekly_timeline": weeks,
        "per_rule": rule_stats,
        "per_category": [{"category": k, "successes": v} for k, v in
                         sorted(by_category.items(), key=lambda kv: -kv[1])],
    }


# --- Write / action tools -------------------------------------------------- #
@mcp.tool
async def book_class(calendar_item_uuid: str) -> dict:
    """Book a class by its ``calendar_item_uuid`` (the ``id`` from
    ``get_schedule``). Idempotent — booking an already-reserved class is a
    no-op. On success, reminder notifications are scheduled. Returns ``ok``,
    ``reservation_id``, and ``message``."""
    result = await pushpress.book(calendar_item_uuid)
    if result.ok:
        await scheduler.reminder_scan_job()
    return result.model_dump(mode="json")


@mcp.tool
async def cancel_reservation(reservation_uuid: str) -> dict:
    """Cancel a booked class by its reservation ``id`` (from
    ``get_reservations``). Returns ``{"ok": bool}``."""
    ok = await pushpress.cancel(reservation_uuid)
    return {"ok": ok}


@mcp.tool
async def create_automation_rule(
    name: str,
    location: str,
    class_category: str,
    day_of_week: int,
    time_of_day: str,
    enabled: bool = True,
    backup_only: bool = False,
) -> dict:
    """Create a rule that auto-books a recurring weekly class the moment its
    booking window opens.

    - ``day_of_week``: 0=Monday .. 6=Sunday.
    - ``time_of_day``: ``"HH:MM"`` — the class start time.
    - ``location`` / ``class_category`` must match the gym's naming (see
      ``get_schedule`` output).
    - ``backup_only=True`` makes a rule that only fires as a fallback in a
      backup chain, never on its own schedule.

    The scheduler is re-armed immediately. Returns the created rule.
    """
    if not 0 <= day_of_week <= 6:
        raise ToolError("day_of_week must be 0 (Monday) .. 6 (Sunday)")
    try:
        hh, mm = (int(x) for x in time_of_day.split(":")[:2])
        tod = time(hour=hh, minute=mm)
    except (ValueError, AttributeError) as e:
        raise ToolError(f"bad time_of_day '{time_of_day}', expected HH:MM") from e
    rule = AutomationRule(
        name=name.strip() or f"{DAYS_LONG[day_of_week]} {class_category}",
        location=location.strip(),
        class_category=class_category.strip(),
        day_of_week=day_of_week,
        time_of_day=tod,
        enabled=enabled,
        backup_only=backup_only,
    )
    with DbSession(engine) as db:
        db.add(rule)
        db.commit()
        db.refresh(rule)
        created = _rule_to_dict(rule)
    await scheduler.horizon_refresh_all()
    logger.info("MCP created automation rule {} ('{}')", created["id"], created["name"])
    return created


@mcp.tool
async def toggle_automation_rule(rule_id: int, enabled: bool) -> dict:
    """Enable or disable an automation rule by id. Re-arms the scheduler.
    Returns the updated rule."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise ToolError(f"no automation rule with id {rule_id}")
        rule.enabled = enabled
        db.add(rule)
        db.commit()
        db.refresh(rule)
        updated = _rule_to_dict(rule)
    await scheduler.horizon_refresh_all()
    return updated


@mcp.tool
async def pause_automation_rule(rule_id: int, paused_until: str | None = None) -> dict:
    """Pause a rule until a date (ISO ``YYYY-MM-DD``), or clear the pause by
    passing an empty/omitted value. Re-arms the scheduler. Returns the updated
    rule."""
    parsed: date | None = None
    if paused_until:
        try:
            parsed = date.fromisoformat(paused_until)
        except ValueError as e:
            raise ToolError(f"bad paused_until, expected YYYY-MM-DD: {e}") from e
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise ToolError(f"no automation rule with id {rule_id}")
        rule.paused_until = parsed
        db.add(rule)
        db.commit()
        db.refresh(rule)
        updated = _rule_to_dict(rule)
    await scheduler.horizon_refresh_all()
    return updated


@mcp.tool
async def delete_automation_rule(rule_id: int) -> dict:
    """Delete an automation rule by id. Any other rule that pointed to this one
    as its backup has that pointer cleared. Re-arms the scheduler."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise ToolError(f"no automation rule with id {rule_id}")
        referring = db.exec(
            select(AutomationRule).where(AutomationRule.backup_rule_id == rule_id)
        ).all()
        for r in referring:
            r.backup_rule_id = None
            db.add(r)
        db.delete(rule)
        db.commit()
    await scheduler.horizon_refresh_all()
    logger.info("MCP deleted automation rule {}", rule_id)
    return {"ok": True, "deleted_rule_id": rule_id}


@mcp.tool
async def fire_automation_rule(rule_id: int) -> dict:
    """Manually trigger a rule right now — books the next matching class in the
    next 14 days. Useful to test a rule. The rule must be enabled. Returns a
    confirmation with the targeted slot."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise ToolError(f"no automation rule with id {rule_id}")
        if not rule.enabled:
            raise ToolError("rule is paused/disabled — enable it before firing")
    logger.info("MCP manually firing rule {} ('{}')", rule_id, rule.name)
    slot, _ = await scheduler.find_next_matching_slot(rule)
    if slot is None:
        raise ToolError("no matching class found in the next 14 days")
    await scheduler.booking_window_job(rule_id, 0, slot.id, manual=True)
    return {
        "ok": True,
        "rule_id": rule_id,
        "targeted_class": slot.name,
        "targeted_start": slot.start.isoformat(),
    }


# --- Login route (auth-exempt, single credential entry point) -------------- #
@mcp.custom_route("/login", methods=["GET", "POST"])
async def mcp_login(request: Request) -> Response:
    """Login/consent screen for the MCP OAuth flow.

    GET renders the form (with the pending ``txn``). POST validates the
    ``.env`` username/password and, on success, resumes the stashed
    authorization — redirecting the browser back to Claude with an auth code.
    """
    if request.method == "GET":
        txn = request.query_params.get("txn", "")
        if not txn:
            return HTMLResponse(_login_html(txn="", error="Missing login session."), status_code=400)
        return HTMLResponse(_login_html(txn=txn))

    form = await request.form()
    txn = str(form.get("txn", ""))
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not txn:
        return HTMLResponse(_login_html(txn="", error="Missing login session."), status_code=400)
    if username != settings.app_username or password != settings.app_password:
        return HTMLResponse(
            _login_html(txn=txn, error="Wrong username or password."), status_code=401
        )
    try:
        redirect_url = await provider.complete_authorize(txn)
    except ValueError as e:
        return HTMLResponse(_login_html(txn="", error=str(e)), status_code=400)
    return RedirectResponse(redirect_url, status_code=303)


def _rule_to_dict(rule: AutomationRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "location": rule.location,
        "class_category": rule.class_category,
        "day_of_week": rule.day_of_week,
        "day_name": DAYS_LONG[rule.day_of_week] if 0 <= rule.day_of_week <= 6 else None,
        "time_of_day": rule.time_of_day.strftime("%H:%M"),
        "enabled": rule.enabled,
        "paused_until": rule.paused_until.isoformat() if rule.paused_until else None,
        "backup_rule_id": rule.backup_rule_id,
        "backup_only": rule.backup_only,
    }


def _login_html(txn: str, error: str | None = None) -> str:
    action = provider.login_url
    err_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Authorize · Domcity Planner</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.green.min.css" />
</head>
<body>
  <main class="container" style="max-width:28rem;margin-top:4rem">
    <article>
      <hgroup>
        <h1>🏛 Domcity Planner</h1>
        <p>Authorize Claude to access your gym planner.</p>
      </hgroup>
      {err_html}
      <form method="post" action="{escape(action)}">
        <input type="hidden" name="txn" value="{escape(txn)}" />
        <label>
          Username
          <input type="text" name="username" autocomplete="username" autofocus required />
        </label>
        <label>
          Password
          <input type="password" name="password" autocomplete="current-password" required />
        </label>
        <button type="submit">Authorize</button>
      </form>
    </article>
  </main>
</body>
</html>"""
