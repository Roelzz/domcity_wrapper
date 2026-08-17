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
async def get_stats() -> dict:
    """Booking-automation statistics with credit context and failure diagnostics.

    Overall success/failure/polling/retry counts, success rate, total and enabled
    rule counts, an 8-week success timeline, per-rule stats, and successful
    bookings per class category — PLUS:

    - ``credits``: your live PushPress budget (``remaining``/``limit``/``used``/
      ``period_start``/``period_end``) for the gating subscription. ``null`` if
      the lookup fails (never blanks the rest of the stats).
    - ``overbooked``: ``true`` when the enabled rules will attempt more bookings
      before ``period_end`` than you have credits left.
    - ``by_reason``: overall breakdown of *why* terminal failures happened
      (``out_of_credits``/``class_full``/``window_not_open``/…).
    - each ``per_rule`` entry also carries ``success_rate``, a ``reasons``
      breakdown, and a ``health`` verdict (``healthy``/``credit_capped``/
      ``loses_capacity_race``/``fires_before_window``/``thrashing``/``no_data``)
      so a 3/199 record reads as "credit_capped", not a mystery."""
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
            reasons: dict[str, int] = {}
            for a in rule_attempts:
                if a.status == "failure":
                    bucket = scheduler._classify_reason(a.message or "")
                    reasons[bucket] = reasons.get(bucket, 0) + 1
            r_decisive = succ + fail
            r_rate = (succ / r_decisive * 100) if r_decisive else 0.0
            rule_stats.append(
                {
                    "rule_id": r.id,
                    "name": r.name,
                    "category": r.class_category,
                    "success": succ,
                    "failure": fail,
                    "success_rate": round(r_rate, 1),
                    "reasons": reasons,
                    "health": _health_verdict(succ, fail, reasons),
                    "last_attempt": last.isoformat() if last else None,
                }
            )
        rule_stats.sort(key=lambda x: -x["success"])

        by_category: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        for a in attempts:
            if a.status == "success":
                rule = next((r for r in rules if r.id == a.rule_id), None)
                if rule:
                    by_category[rule.class_category] = by_category.get(rule.class_category, 0) + 1
            elif a.status == "failure":
                bucket = scheduler._classify_reason(a.message or "")
                by_reason[bucket] = by_reason.get(bucket, 0) + 1

    # Credit context — fail-open: a broken lookup returns null, never blanks stats.
    credits: dict | None = None
    overbooked = False
    try:
        usage = await pushpress.list_subscription_usage()
        gating = scheduler._gating_usage(usage)
        if gating is not None:
            credits = {
                "plan": gating.plan,
                "limit": gating.limit,
                "used": gating.used,
                "reservations": gating.reservations,
                "checkins": gating.checkins,
                "remaining": gating.remaining,
                "period_start": gating.period_start,
                "period_end": gating.period_end,
            }
            if gating.remaining is not None:
                total, _ = await scheduler.scheduled_bookings_before_period_end(usage)
                overbooked = total > gating.remaining
    except Exception as e:
        logger.warning("get_stats: credit lookup failed: {}", e)

    return {
        "n_success": n_success,
        "n_failure": n_failure,
        "n_polling": n_polling,
        "n_retry": n_retry,
        "success_rate": round(success_rate, 1),
        "total_rules": len(rules),
        "enabled_count": enabled_count,
        "credits": credits,
        "overbooked": overbooked,
        "weekly_timeline": weeks,
        "per_rule": rule_stats,
        "by_reason": [{"reason": k, "count": v} for k, v in
                      sorted(by_reason.items(), key=lambda kv: -kv[1])],
        "per_category": [{"category": k, "successes": v} for k, v in
                         sorted(by_category.items(), key=lambda kv: -kv[1])],
    }


@mcp.tool
async def get_automation_forecast() -> dict:
    """Will the enabled automations overrun your session-credit budget before the
    billing period resets? Read-only forecast — books nothing.

    Compares ``remaining`` credits against how many bookings the enabled,
    non-paused rules will attempt on or before ``period_end`` (slots you have
    already booked are excluded). Returns ``credits``, ``period_end``,
    ``scheduled_before_period_end`` (a count plus a per-rule slot list annotated
    with each rule's ``health``), an ``overbooked`` flag, a ``recommendation``
    naming the worst-health rule to pause (when overbooked), and a plain-language
    ``summary``. Fails open: ``credits`` is ``null`` if the lookup errors."""
    try:
        usage = await pushpress.list_subscription_usage()
    except Exception as e:
        logger.warning("get_automation_forecast: usage lookup failed: {}", e)
        return {
            "credits": None,
            "period_end": None,
            "scheduled_before_period_end": {"count": 0, "per_rule": []},
            "overbooked": False,
            "recommendation": None,
            "summary": "Credit lookup failed — cannot forecast right now.",
        }

    gating = scheduler._gating_usage(usage)
    total, per_rule = await scheduler.scheduled_bookings_before_period_end(usage)
    remaining = gating.remaining if gating else None
    period_end = gating.period_end if gating else None
    overbooked = remaining is not None and total > remaining

    health = _rule_health_map([r["rule_id"] for r in per_rule])
    for r in per_rule:
        h = health.get(r["rule_id"], {})
        r["health"] = h.get("health", "no_data")
        r["success_rate"] = h.get("success_rate", 0.0)

    recommendation = None
    if overbooked and per_rule:
        worst = min(
            per_rule,
            key=lambda r: (_HEALTH_RANK.get(r["health"], 9), r["success_rate"]),
        )
        recommendation = {
            "rule_id": worst["rule_id"],
            "name": worst["name"],
            "health": worst["health"],
            "action": (
                f"consider pausing rule {worst['rule_id']} ({worst['name']}) "
                f"via pause_automation_rule"
            ),
        }

    if remaining is None:
        summary = (
            f"No finite session cap detected — {total} booking(s) scheduled "
            f"before {period_end or 'period end'}, budget looks unlimited."
        )
    elif overbooked:
        summary = (
            f"Overbooked: {total} booking(s) scheduled before {period_end} but "
            f"only {remaining} credit(s) left. "
            + (recommendation["action"] + "." if recommendation else "")
        )
    else:
        summary = (
            f"On track: {total} booking(s) scheduled before {period_end} and "
            f"{remaining} credit(s) remaining."
        )

    credits = None
    if gating is not None:
        credits = {
            "plan": gating.plan,
            "limit": gating.limit,
            "used": gating.used,
            "remaining": gating.remaining,
            "period_start": gating.period_start,
            "period_end": gating.period_end,
        }

    return {
        "credits": credits,
        "period_end": period_end,
        "scheduled_before_period_end": {"count": total, "per_rule": per_rule},
        "overbooked": overbooked,
        "recommendation": recommendation,
        "summary": summary,
    }


@mcp.tool
async def get_workout_of_day(date: str, class_type_uid: str | None = None) -> list[dict]:
    """Get the workout of the day for a class type on a given date (ISO
    ``YYYY-MM-DD``).

    Returns workout metadata: ``uid``, ``workoutUid``, ``workoutState``,
    ``imageUrl``, ``videoUrlId``, etc.

    **Does NOT include exercises, sets, reps, or warmup data** — those are not
    exposed via the member-facing PushPress GraphQL API.

    Use ``get_class_types()`` to find the ``id`` (class type UID) for each
    class type. Common UIDs:
        - Classic CrossFit:      4ebe07a3-b8f0-41ba-8e34-8d4cc2a09014
        - Functional CrossFit:   8e5604a1-463b-4316-bce8-abdee466dabc
        - Olympic Weightlifting: a2002767-a66a-4894-a76e-4fe19bb33b20
        - Strength:              db209335-5cc6-4a9f-a611-64a10fe6b1b3
        - Hyrox:                 84d97a3b-14b1-4efb-ae0c-6b7ba1b51438
    """
    return await pushpress.get_workout_of_day(date, class_type_uid)


@mcp.tool
async def get_workout_scores(workout_part_uid: str, workout_uid: str) -> dict:
    """Get member scores for a workout part.

    Returns ``{scores: [...], topScore: ... | null}`` where each score has
    ``sets: [{weight, reps}]``.

    **NOTE:** Returns empty ``scores`` arrays when no scores have been logged
    in PushPress. The ``workoutPartUid`` is not exposed via the member API —
    ``getWorkoutPart`` returns null for all known UIDs.
    """
    return await pushpress.get_workout_scores(workout_part_uid, workout_uid)


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
    """Manually trigger a rule right now — attempts to book the next matching
    class in the next 14 days. Useful to test a rule. The rule must be enabled.

    Returns the targeted slot plus the actual ``outcome`` of the attempt, so a
    credit skip surfaces clearly (e.g. ``"skipped: out of credits, period ends
    2026-07-16"``) instead of a vague OK. ``booked`` is ``true`` only when the
    booking succeeded."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise ToolError(f"no automation rule with id {rule_id}")
        if not rule.enabled:
            raise ToolError("rule is paused/disabled — enable it before firing")
    logger.info("MCP manually firing rule {} ('{}')", rule_id, rule.name)
    slot, _ = await scheduler.find_next_matching_slot(rule)
    if slot is None:
        raise ToolError(
            "no bookable class found in the next 14 days "
            "(any matches may already be booked)"
        )
    outcome = await scheduler.booking_window_job(rule_id, 0, slot.id, manual=True)
    return {
        "ok": True,
        "booked": outcome == "booked",
        "outcome": outcome,
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


_HEALTH_RANK = {
    "credit_capped": 0,
    "loses_capacity_race": 1,
    "fires_before_window": 2,
    "thrashing": 3,
    "healthy": 4,
    "no_data": 5,
}


def _health_verdict(succ: int, fail: int, reasons: dict[str, int]) -> str:
    """Derive a rule's health from its decisive attempts (no schema change).

    Terminal-decisive = success + terminal failure (retry/polling excluded, as
    the caller only feeds those two). ≥70% success is ``healthy``; otherwise the
    dominant failure reason names the pathology so a bad record is actionable."""
    decisive = succ + fail
    if decisive == 0:
        return "no_data"
    if (succ / decisive) >= 0.70:
        return "healthy"
    if not reasons:
        return "thrashing"
    dominant = max(reasons.items(), key=lambda kv: kv[1])[0]
    return {
        "out_of_credits": "credit_capped",
        "class_full": "loses_capacity_race",
        "window_not_open": "fires_before_window",
    }.get(dominant, "thrashing")


def _rule_health_map(rule_ids: list[int]) -> dict[int, dict]:
    """Compute ``{rule_id: {success_rate, health}}`` from historical attempts."""
    if not rule_ids:
        return {}
    with DbSession(engine) as db:
        attempts = db.exec(
            select(BookingAttempt).where(BookingAttempt.rule_id.in_(rule_ids))
        ).all()
    out: dict[int, dict] = {}
    for rid in rule_ids:
        ra = [a for a in attempts if a.rule_id == rid]
        succ = sum(1 for a in ra if a.status == "success")
        fail = sum(1 for a in ra if a.status == "failure")
        reasons: dict[str, int] = {}
        for a in ra:
            if a.status == "failure":
                bucket = scheduler._classify_reason(a.message or "")
                reasons[bucket] = reasons.get(bucket, 0) + 1
        r_dec = succ + fail
        out[rid] = {
            "success_rate": round((succ / r_dec * 100) if r_dec else 0.0, 1),
            "health": _health_verdict(succ, fail, reasons),
        }
    return out


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
