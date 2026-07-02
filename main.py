from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlmodel import Session as DbSession
from sqlmodel import select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import pushpress
import scheduler
from auth import COOKIE_NAME, AuthMiddleware, make_session_token
from mcp_server import mcp, provider
from models import AutomationRule, BookingAttempt, engine, init_db
from settings import settings

logger.remove()
logger.add(
    sink=lambda msg: print(msg, end=""),
    level=settings.log_level,
    format="{time:DD-MM-YYYY at HH:mm:ss} | {level: <8} | {message}",
)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SLOT_SEPARATOR = "|"

# Streamable-HTTP ASGI app for the MCP server. Built with path="/" and mounted
# at "/mcp" below, so the externally reachable endpoint is <mcp_base_url>/mcp.
# Its lifespan (which boots the MCP session manager) MUST be chained into ours.
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_app.lifespan(app):
        init_db()
        scheduler.start()
        await _prime_token()
        await scheduler.horizon_refresh_all()
        scheduler.schedule_token_refresh()
        scheduler.schedule_reminder_scan()
        scheduler.schedule_daily_digest()
        scheduler.schedule_credit_warning()
        scheduler.schedule_horizon_refresh()
        logger.info("Domcity Planner up on port {}", settings.port)
        yield
        scheduler.shutdown()
        await pushpress.aclose()


async def _prime_token() -> None:
    if not (settings.pushpress_email and settings.pushpress_password):
        logger.warning("PUSHPRESS_EMAIL/PASSWORD not set — API calls will fail")
        return
    try:
        await pushpress.ensure_token()
    except Exception as e:
        # Silent on Telegram — every container restart would otherwise spam.
        # If the failure is persistent, the daily refresh cron and any
        # actual booking attempt will surface the problem via the digest.
        logger.error("Initial token load failed: {}", e)


app = FastAPI(title="Domcity Planner", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
# Outermost: trust the reverse proxy (Coolify/Traefik) X-Forwarded-* headers so
# the app sees the real https scheme. Without this, uvicorn only trusts
# 127.0.0.1 and treats proxied requests as http, which downgrades the OAuth
# discovery/redirect URLs to http and breaks the Claude connector handshake.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

# OAuth discovery metadata for the MCP server must live at the domain root
# (RFC 8414 / 9728), while the operational + protocol routes live under /mcp.
app.router.routes.extend(provider.get_well_known_routes(mcp_path="/"))
app.mount("/mcp", mcp_app)


# ---------- Health ----------
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---------- Auth ----------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if username != settings.app_username or password != settings.app_password:
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        make_session_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------- Schedule ----------
@app.get("/", response_class=HTMLResponse)
async def home():
    return RedirectResponse("/schedule", status_code=303)


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(
    request: Request,
    start: str | None = None,
    locations: str | None = None,
    categories: str | None = None,
):
    start_d = _parse_date(start) or _monday_of(datetime.now().date())
    end_d = start_d + timedelta(days=6)
    all_classes, err = await _fetch_schedule(start_d, end_d)

    reservation_ids = await _booked_class_ids()
    for c in all_classes:
        if c.id in reservation_ids:
            c.booked = True

    all_locations = sorted({c.location for c in all_classes if c.location})
    all_categories = sorted({c.category for c in all_classes if c.category})

    loc_filter = set(_split_csv(locations))
    cat_filter = set(_split_csv(categories))
    filtered = [
        c for c in all_classes
        if (not loc_filter or c.location in loc_filter)
        and (not cat_filter or c.category in cat_filter)
    ]

    # Compute booking-window-open hints for each class. If the window is in
    # the future, the card shows "Opens in 3d 14h" instead of a Book button.
    now_local = datetime.now(scheduler.tz())
    window_hints: dict[str, str] = {}
    for c in filtered:
        opens_at = scheduler.window_open_time(c)
        if opens_at > now_local:
            window_hints[c.id] = _humanize_until(opens_at - now_local)

    by_day = defaultdict(list)
    for c in filtered:
        by_day[c.start.date()].append(c)
    days = []
    for i in range(7):
        d = start_d + timedelta(days=i)
        days.append({
            "date": d,
            "label_short": DAYS[d.weekday()],
            "label_long": DAYS_LONG[d.weekday()],
            "is_today": d == datetime.now().date(),
            "classes": sorted(by_day.get(d, []), key=lambda c: c.start),
        })

    # Primary rules (for the "Set as backup of…" dropdown on each class card).
    with DbSession(engine) as db:
        primary_rules = db.exec(
            select(AutomationRule)
            .where(AutomationRule.backup_only == False)  # noqa: E712
            .order_by(AutomationRule.day_of_week)
        ).all()

    ctx = {
        "active": "schedule",
        "start": start_d,
        "prev_start": (start_d - timedelta(days=7)).isoformat(),
        "next_start": (start_d + timedelta(days=7)).isoformat(),
        "today_start": _monday_of(datetime.now().date()).isoformat(),
        "days": days,
        "all_locations": all_locations,
        "all_categories": all_categories,
        "selected_locations": loc_filter,
        "selected_categories": cat_filter,
        "filters_qs": _filter_query(loc_filter, cat_filter),
        "window_hints": window_hints,
        "total_count": len(filtered),
        "unfiltered_count": len(all_classes),
        "primary_rules": primary_rules,
        "error": err,
        "token_warning": _token_warning(),
    }
    return templates.TemplateResponse(request, "schedule.html", ctx)


@app.post("/schedule/book/{slot_id}", response_class=HTMLResponse)
async def book_slot(slot_id: str):
    try:
        result = await pushpress.book(slot_id)
        if not result.ok:
            return HTMLResponse(
                f'<span class="error">Failed: {result.message}</span>', status_code=400
            )
        await scheduler.reminder_scan_job()  # schedule reminders for the new booking
        return HTMLResponse('<span class="success">Booked ✓</span>')
    except Exception as e:
        logger.exception("book failed")
        return HTMLResponse(f'<span class="error">{e}</span>', status_code=500)


# ---------- Reservations ----------
@app.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request):
    items, err = await _fetch_reservations()
    ctx = {
        "active": "reservations",
        "reservations": items,
        "error": err,
        "token_warning": _token_warning(),
    }
    return templates.TemplateResponse(request, "reservations.html", ctx)


@app.get("/calendar.ics")
async def calendar_ics(token: str = ""):
    """iCalendar feed of active reservations. Subscribe in Apple Calendar /
    Google / Fastmail. Token-gated with the app password since calendar
    clients can't carry the session cookie."""
    if not token or token != settings.app_password:
        raise HTTPException(status_code=403, detail="bad token")
    if not (settings.pushpress_email and settings.pushpress_password):
        return Response("", media_type="text/calendar")
    try:
        reservations = await pushpress.list_reservations()
    except Exception:
        logger.exception("calendar.ics: list_reservations failed")
        reservations = []
    body = _render_ical(reservations)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, max-age=300"},
    )


def _render_ical(reservations) -> str:
    """Hand-rolled VCALENDAR. One VEVENT per reservation."""
    now_utc = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Domcity Planner//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Domcity Planner",
        "X-WR-TIMEZONE:" + settings.tz,
    ]
    for r in reservations:
        if not r.cancellable:
            continue
        start = r.start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        end = r.end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        summary = _ical_escape(r.class_name)
        location = _ical_escape(r.location or "")
        description_bits = []
        if r.instructor:
            description_bits.append(f"Coach: {r.instructor}")
        description = _ical_escape("\\n".join(description_bits))
        lines += [
            "BEGIN:VEVENT",
            f"UID:{r.id}@domcity.local",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{summary}",
        ]
        if location:
            lines.append(f"LOCATION:{location}")
        if description:
            lines.append(f"DESCRIPTION:{description}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _ical_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@app.post("/reservations/{reservation_id}/cancel", response_class=HTMLResponse)
async def cancel_reservation(reservation_id: str):
    try:
        ok = await pushpress.cancel(reservation_id)
        if not ok:
            return HTMLResponse('<span class="error">Cancel failed</span>', status_code=400)
        return HTMLResponse("")
    except Exception as e:
        logger.exception("cancel failed")
        return HTMLResponse(f'<span class="error">{e}</span>', status_code=500)


# ---------- Automation ----------
@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    """Read-only dashboard: success rate, weekly timeline, per-rule + per-category."""
    with DbSession(engine) as db:
        attempts = db.exec(select(BookingAttempt)).all()
        rules = db.exec(select(AutomationRule)).all()

    # Overall
    n_success = sum(1 for a in attempts if a.status == "success")
    n_failure = sum(1 for a in attempts if a.status == "failure")
    n_polling = sum(1 for a in attempts if a.status == "polling")
    n_retry = sum(1 for a in attempts if a.status == "retry")
    decisive = n_success + n_failure
    success_rate = (n_success / decisive * 100) if decisive else 0.0
    enabled_count = sum(1 for r in rules if r.enabled)

    # Weekly timeline — last 8 weeks, successes per week
    now_local = datetime.now(scheduler.tz())
    # Anchor on Monday of "this week"
    this_monday = (now_local.date() - timedelta(days=now_local.weekday()))
    weeks = []
    for i in range(8):
        wk_start = this_monday - timedelta(weeks=7 - i)
        wk_end = wk_start + timedelta(days=7)
        count = sum(
            1 for a in attempts
            if a.status == "success" and wk_start <= a.fired_at.date() < wk_end
        )
        weeks.append({"label": wk_start.strftime("%d %b"), "count": count})
    max_count = max((w["count"] for w in weeks), default=0)

    # Per rule
    rule_stats = []
    for r in rules:
        rule_attempts = [a for a in attempts if a.rule_id == r.id]
        succ = sum(1 for a in rule_attempts if a.status == "success")
        fail = sum(1 for a in rule_attempts if a.status == "failure")
        last = max((a.fired_at for a in rule_attempts), default=None)
        rule_stats.append({
            "rule": r,
            "success": succ,
            "failure": fail,
            "last_attempt": last,
        })
    rule_stats.sort(key=lambda x: -x["success"])

    # Per category — successful bookings
    by_category: dict[str, int] = defaultdict(int)
    for a in attempts:
        if a.status != "success":
            continue
        # target_class format: "<rule.name> — <Day DD Mon HH:MM>"
        # Map to category via rule
        rule = next((r for r in rules if r.id == a.rule_id), None)
        if rule:
            by_category[rule.class_category] += 1
    categories_sorted = sorted(by_category.items(), key=lambda kv: -kv[1])

    ctx = {
        "active": "stats",
        "n_success": n_success,
        "n_failure": n_failure,
        "n_polling": n_polling,
        "n_retry": n_retry,
        "success_rate": success_rate,
        "total_rules": len(rules),
        "enabled_count": enabled_count,
        "weeks": weeks,
        "max_count": max_count,
        "rule_stats": rule_stats,
        "categories_sorted": categories_sorted,
        "token_warning": _token_warning(),
    }
    return templates.TemplateResponse(request, "stats.html", ctx)


@app.get("/automation", response_class=HTMLResponse)
async def automation_page(
    request: Request,
    prefill_name: str | None = None,
    prefill_location: str | None = None,
    prefill_category: str | None = None,
    prefill_day: int | None = None,
    prefill_time: str | None = None,
    backup_for: int | None = None,
):
    with DbSession(engine) as db:
        all_rules = db.exec(
            select(AutomationRule).order_by(AutomationRule.day_of_week)
        ).all()
        attempts = db.exec(
            select(BookingAttempt).order_by(BookingAttempt.fired_at.desc()).limit(20)
        ).all()
        backup_for_rule = db.get(AutomationRule, backup_for) if backup_for else None
    if backup_for and backup_for_rule is None:
        raise HTTPException(404, "primary rule not found")

    # Top-level list = primary (non-backup_only) rules. Backup-only rules are
    # rendered nested under whichever primary owns them via the chain map.
    rules = [r for r in all_rules if not r.backup_only]
    rules_by_id = {r.id: r for r in all_rules}

    # Build the chain map: rule_id -> ordered list of backup rules (the chain
    # starting at this rule, excluding the rule itself). Stop at cycles / cap.
    backup_chains: dict[int, list[AutomationRule]] = {}
    for r in rules:
        chain: list[AutomationRule] = []
        seen = {r.id}
        cur = r
        while cur.backup_rule_id is not None and len(chain) < scheduler.MAX_CHAIN_DEPTH:
            nxt = rules_by_id.get(cur.backup_rule_id)
            if nxt is None or nxt.id in seen:
                break
            chain.append(nxt)
            seen.add(nxt.id)
            cur = nxt
        backup_chains[r.id] = chain

    classes = await _fetch_classes_for_automation()
    categories = sorted({c.category for c in classes if c.category})

    # Reservations let us mark which queued slots are already booked.
    try:
        reservations = await pushpress.list_reservations()
        booked_class_ids = {res.class_id for res in reservations}
    except Exception:
        booked_class_ids = set()

    next_fires: dict[int, str] = {}
    horizons: dict[int, dict] = {}
    now = datetime.now(scheduler.tz())
    for r in rules:
        if not r.enabled:
            continue
        fire = _compute_next_fire_from(classes, r, now)
        next_fires[r.id] = fire.isoformat() if fire else "—"
        # Horizon: count booked vs queued matching slots in the 14d lookahead.
        matches = scheduler.find_all_matching_slots(r, classes, now)
        booked = sum(1 for s in matches if s.id in booked_class_ids)
        queued = len(matches) - booked
        horizons[r.id] = {
            "total": len(matches),
            "booked": booked,
            "queued": queued,
        }

    # In backup mode, default the cascading form's day/time from the primary
    # rule (the typical case: same-day-same-time backup at a different location
    # or for a different training). User can still change either selector.
    effective_prefill_day = prefill_day
    effective_prefill_time = prefill_time
    effective_prefill_location = prefill_location
    if backup_for_rule is not None and prefill_day is None and prefill_time is None:
        effective_prefill_day = backup_for_rule.day_of_week
        effective_prefill_time = backup_for_rule.time_of_day.strftime("%H:%M")

    # Compute the cascading day/time block from prefill (or empty)
    selected_slot = ""
    if effective_prefill_location and effective_prefill_time:
        selected_slot = f"{effective_prefill_location}{SLOT_SEPARATOR}{effective_prefill_time}"
    day_time_ctx = _build_day_time_ctx(
        classes,
        category=prefill_category or "",
        dow=effective_prefill_day if effective_prefill_day is not None else None,
        selected_slot=selected_slot,
    )

    ctx = {
        "active": "automation",
        "rules": rules,
        "backup_chains": backup_chains,
        "attempts": attempts,
        "next_fires": next_fires,
        "horizons": horizons,
        "days": list(enumerate(DAYS_LONG)),
        "categories": categories,
        "day_time": day_time_ctx,
        "prefill": {
            "name": prefill_name or "",
            "category": prefill_category or "",
        },
        "backup_for_rule": backup_for_rule,
        "token_warning": _token_warning(),
    }
    return templates.TemplateResponse(request, "automation.html", ctx)


def _build_day_time_ctx(classes, category: str, dow, selected_slot: str) -> dict:
    """Compute the cascading-dropdown context. Auto-narrows the day list to
    days that have this training, auto-picks if only one. Same for time slot."""
    available_days: set[int] = set()
    slots: list[tuple[str, str]] = []
    auto_day = False
    auto_slot = False
    if category:
        cat = category.lower()
        available_days = {c.start.weekday() for c in classes if c.category.lower() == cat}
        # If the currently-picked day isn't valid for this training, clear it
        if dow is not None and dow not in available_days:
            dow = None
        # Auto-pick the day if there's only one
        if dow is None and len(available_days) == 1:
            dow = next(iter(available_days))
            auto_day = True
        if dow is not None and dow in available_days:
            slots = sorted({
                (c.location, c.start.strftime("%H:%M")) for c in classes
                if c.category.lower() == cat and c.start.weekday() == dow and c.location
            }, key=lambda lt: (lt[1], lt[0]))
            if len(slots) == 1 and not selected_slot:
                loc, t = slots[0]
                selected_slot = f"{loc}{SLOT_SEPARATOR}{t}"
                auto_slot = True
    return {
        "available_days": available_days,
        "selected_day": dow if dow is not None else "",
        "training_picked": bool(category),
        "slots": slots,
        "selected_slot": selected_slot,
        "selectors_complete": bool(category and dow is not None),
        "ready": bool(slots),
        "auto_day": auto_day,
        "auto_slot": auto_slot,
        "all_days": list(enumerate(DAYS_LONG)),
    }


async def _fetch_classes_for_automation():
    """Single 14-day fetch reused for all automation-page computations."""
    if not (settings.pushpress_email and settings.pushpress_password):
        return []
    today = datetime.now().date()
    try:
        return await pushpress.list_schedule(today, today + timedelta(days=14))
    except Exception:
        logger.exception("automation prefetch failed")
        return []


def _compute_next_fire_from(classes, rule: AutomationRule, now):
    """Look through already-fetched classes for the next match; return when
    that class's booking window opens (or now if already open)."""
    matches = []
    for c in classes:
        if rule.location and c.location.lower() != rule.location.lower():
            continue
        if rule.class_category and c.category.lower() != rule.class_category.lower():
            continue
        local = c.start.astimezone(scheduler.tz())
        if local.weekday() != rule.day_of_week:
            continue
        if local.hour != rule.time_of_day.hour or local.minute != rule.time_of_day.minute:
            continue
        if local <= now:
            continue
        if rule.paused_until and local.date() <= rule.paused_until:
            continue
        matches.append(c)
    if not matches:
        return None
    slot = min(matches, key=lambda c: c.start)
    fire = scheduler.window_open_time(slot)
    return max(fire, now)


@app.get("/automation/refresh-form", response_class=HTMLResponse)
async def automation_refresh_form(
    request: Request,
    class_category: str = "",
    day_of_week: str = "",
    time_of_day: str = "",
):
    """HTMX partial: returns the cascading Day + Time-slot block.
    Triggered when training or day changes. Auto-narrows both selects and
    auto-picks when only one option exists at each level."""
    try:
        dow: int | None = int(day_of_week) if day_of_week != "" else None
    except ValueError:
        dow = None
    classes = await _fetch_classes_for_automation()
    ctx = _build_day_time_ctx(classes, category=class_category, dow=dow, selected_slot=time_of_day)
    return templates.TemplateResponse(request, "_automation_day_time.html", ctx)


# Backwards-compat shim: the form template used to hit /automation/time-slots
# directly. Keeping this alias makes a stale cached page degrade gracefully.
@app.get("/automation/time-slots", response_class=HTMLResponse)
async def automation_time_slots_compat(
    request: Request,
    class_category: str = "",
    day_of_week: str = "",
    time_of_day: str = "",
):
    return await automation_refresh_form(
        request,
        class_category=class_category,
        day_of_week=day_of_week,
        time_of_day=time_of_day,
    )


@app.get("/automation/from-class/{slot_id}")
async def automation_from_class(slot_id: str):
    """Look up a class slot and redirect to /automation with the form pre-filled."""
    if not (settings.pushpress_email and settings.pushpress_password):
        return RedirectResponse("/automation", status_code=303)
    today = datetime.now().date()
    classes = await pushpress.list_schedule(
        today - timedelta(days=today.weekday()), today + timedelta(days=14)
    )
    slot = next((c for c in classes if c.id == slot_id), None)
    if not slot:
        return RedirectResponse("/automation", status_code=303)
    suggested_name = f"{DAYS_LONG[slot.start.weekday()]} {slot.category}"
    if slot.location_code:
        suggested_name += f" {slot.location_code}"
    qs = urlencode({
        "prefill_name": suggested_name,
        "prefill_location": slot.location,
        "prefill_category": slot.category,
        "prefill_day": slot.start.weekday(),
        "prefill_time": slot.start.strftime("%H:%M"),
    })
    return RedirectResponse(f"/automation?{qs}", status_code=303)


@app.post("/automation")
async def automation_create(
    name: str = Form(...),
    class_category: str = Form(...),
    day_of_week: int = Form(...),
    time_of_day: str = Form(...),  # format: "Location|HH:MM"
):
    try:
        location, hhmm = time_of_day.split(SLOT_SEPARATOR, 1)
        hh, mm = (int(x) for x in hhmm.split(":")[:2])
    except (ValueError, AttributeError) as e:
        raise HTTPException(400, f"Bad time slot: {e}") from e
    rule = AutomationRule(
        name=name.strip() or f"{DAYS_LONG[day_of_week]} {class_category}",
        location=location.strip(),
        class_category=class_category.strip(),
        day_of_week=day_of_week,
        time_of_day=time(hour=hh, minute=mm),
        enabled=True,
    )
    with DbSession(engine) as db:
        db.add(rule)
        db.commit()
        db.refresh(rule)
    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/toggle")
async def automation_toggle(rule_id: int):
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise HTTPException(404)
        rule.enabled = not rule.enabled
        db.add(rule)
        db.commit()
    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/delete")
async def automation_delete(rule_id: int):
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if rule:
            # Cascade-clear backup_rule_id pointers pointing at this rule so
            # no other rule is left dangling.
            referring = db.exec(
                select(AutomationRule).where(AutomationRule.backup_rule_id == rule_id)
            ).all()
            for r in referring:
                r.backup_rule_id = None
                db.add(r)
            db.delete(rule)
            db.commit()
    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/pause-until")
async def automation_pause_until(rule_id: int, paused_until: str = Form("")):
    """Set or clear the per-rule pause-until date. Empty string clears."""
    parsed: date | None = None
    if paused_until:
        try:
            parsed = datetime.fromisoformat(paused_until).date()
        except ValueError as e:
            raise HTTPException(400, f"bad date: {e}") from e
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise HTTPException(404)
        rule.paused_until = parsed
        db.add(rule)
        db.commit()
    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/fire")
async def automation_fire(rule_id: int):
    """Manually trigger booking_window_job for this rule right now. Useful
    for testing or to force an immediate attempt when the cron hasn't fired
    yet. Resolves the next matching slot first so the job has its required
    calendar_item_uuid; bypasses the loop guard via manual=True."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise HTTPException(404)
        if not rule.enabled:
            raise HTTPException(400, "rule is paused")
    logger.info("Manually firing rule {} ('{}')", rule_id, rule.name)
    slot, _ = await scheduler.find_next_matching_slot(rule)
    if slot is None:
        raise HTTPException(400, "no matching class found in next 14 days")
    await scheduler.booking_window_job(rule_id, 0, slot.id, manual=True)
    return RedirectResponse("/automation", status_code=303)


def _attach_backup_to_chain_tail(
    db, primary_id: int, new_rule: AutomationRule
) -> None:
    """Walk the existing chain from `primary_id` and attach `new_rule` (already
    persisted with an id) to the tail. Enforces MAX_CHAIN_DEPTH; raises 400 if
    the chain is already full. Caller commits."""
    primary = db.get(AutomationRule, primary_id)
    if not primary:
        raise HTTPException(404)
    tail = primary
    depth = 1
    visited = {primary.id}
    while tail.backup_rule_id is not None:
        if depth >= scheduler.MAX_CHAIN_DEPTH:
            raise HTTPException(400, "chain already at depth cap")
        nxt = db.get(AutomationRule, tail.backup_rule_id)
        if nxt is None or nxt.id in visited:
            break
        visited.add(nxt.id)
        tail = nxt
        depth += 1
    tail.backup_rule_id = new_rule.id
    db.add(tail)


@app.post("/automation/{rule_id}/add-backup")
async def automation_add_backup(
    rule_id: int,
    name: str = Form(...),
    class_category: str = Form(...),
    day_of_week: int = Form(...),
    time_of_day: str = Form(...),  # "Location|HH:MM" (same as automation_create)
):
    """Create a new backup_only rule and attach it to the tail of this rule's
    existing backup chain. Uses the same packed Location|HH:MM contract as
    automation_create so the cascading HTMX picker can submit to either."""
    try:
        location, hhmm = time_of_day.split(SLOT_SEPARATOR, 1)
        hh, mm = (int(x) for x in hhmm.split(":")[:2])
    except (ValueError, AttributeError) as e:
        raise HTTPException(400, f"Bad time slot: {e}") from e

    new_rule = AutomationRule(
        name=name.strip() or f"{DAYS_LONG[day_of_week]} {class_category}",
        location=location.strip(),
        class_category=class_category.strip(),
        day_of_week=day_of_week,
        time_of_day=time(hour=hh, minute=mm),
        enabled=True,
        backup_only=True,
    )
    with DbSession(engine) as db:
        db.add(new_rule)
        db.flush()  # populate new_rule.id
        _attach_backup_to_chain_tail(db, rule_id, new_rule)
        db.commit()

    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/from-class/{slot_id}/as-backup")
async def automation_from_class_as_backup(
    slot_id: str,
    primary_rule_id: int = Form(...),
):
    """Make this specific class a backup of the selected primary rule. Looks
    up the slot in PushPress, creates a backup_only AutomationRule with the
    slot's location/category/day/time, attaches to the primary's chain tail."""
    if not (settings.pushpress_email and settings.pushpress_password):
        raise HTTPException(503, "PushPress credentials not configured")
    today = datetime.now().date()
    classes = await pushpress.list_schedule(
        today - timedelta(days=today.weekday()), today + timedelta(days=14)
    )
    slot = next((c for c in classes if c.id == slot_id), None)
    if not slot:
        raise HTTPException(404, "class not found in upcoming schedule")
    local = slot.start.astimezone(scheduler.tz())
    suggested_name = f"{DAYS_LONG[local.weekday()]} {slot.category}"
    if slot.location_code:
        suggested_name += f" {slot.location_code}"
    new_rule = AutomationRule(
        name=suggested_name,
        location=slot.location,
        class_category=slot.category,
        day_of_week=local.weekday(),
        time_of_day=time(hour=local.hour, minute=local.minute),
        enabled=True,
        backup_only=True,
    )
    with DbSession(engine) as db:
        db.add(new_rule)
        db.flush()
        _attach_backup_to_chain_tail(db, primary_rule_id, new_rule)
        db.commit()

    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/remove-backup")
async def automation_remove_backup(rule_id: int):
    """Remove the immediate backup of this rule. Deletes the backup_only rule
    pointed to and re-parents its own backup (if any) to this rule so the rest
    of the chain stays intact."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule:
            raise HTTPException(404)
        if rule.backup_rule_id is None:
            raise HTTPException(400, "no backup to remove")
        backup = db.get(AutomationRule, rule.backup_rule_id)
        # Re-parent: rule's new backup is the removed backup's own backup.
        rule.backup_rule_id = backup.backup_rule_id if backup else None
        db.add(rule)
        if backup is not None and backup.backup_only:
            # Only delete it if we created it as a dedicated backup. Standalone
            # rules that someone wired up as a backup should be left alone.
            db.delete(backup)
        db.commit()
    await scheduler.horizon_refresh_all()
    return RedirectResponse("/automation", status_code=303)


# ---------- Helpers ----------
def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _humanize_until(delta: timedelta) -> str:
    """'3d 14h' / '4h 12m' / '47m'. Always rounds toward zero of the smaller
    unit so the displayed time never overstates how much remains."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "now"
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes = total // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _filter_query(locs: set[str], cats: set[str]) -> str:
    parts: dict[str, str] = {}
    if locs:
        parts["locations"] = ",".join(sorted(locs))
    if cats:
        parts["categories"] = ",".join(sorted(cats))
    return urlencode(parts)


async def _fetch_schedule(start_d: date, end_d: date):
    if not (settings.pushpress_email and settings.pushpress_password):
        return [], "PUSHPRESS_TOKEN not set — see .env.example for how to obtain it."
    try:
        items = await pushpress.list_schedule(start_d, end_d)
        return items, None
    except Exception as e:
        logger.exception("schedule fetch failed")
        return [], str(e)


async def _fetch_reservations():
    if not (settings.pushpress_email and settings.pushpress_password):
        return [], "PUSHPRESS_TOKEN not set."
    try:
        return await pushpress.list_reservations(), None
    except Exception as e:
        logger.exception("reservations fetch failed")
        return [], str(e)


async def _booked_class_ids() -> set[str]:
    if not (settings.pushpress_email and settings.pushpress_password):
        return set()
    try:
        return {r.class_id for r in await pushpress.list_reservations() if r.class_id}
    except Exception:
        return set()


async def _time_slots_for(category: str, day_of_week: int) -> list[tuple[str, str]]:
    """Return unique (location, HH:MM) tuples for upcoming classes matching
    the (category, day) combo. Sorted by time then location for easy scanning."""
    if not (settings.pushpress_email and settings.pushpress_password):
        return []
    today = datetime.now().date()
    try:
        items = await pushpress.list_schedule(today, today + timedelta(days=14))
    except Exception:
        logger.exception("time slots fetch failed")
        return []
    seen: set[tuple[str, str]] = set()
    for c in items:
        if c.category.lower() != category.lower():
            continue
        if c.start.weekday() != day_of_week:
            continue
        if not c.location:
            continue
        seen.add((c.location, c.start.strftime("%H:%M")))
    return sorted(seen, key=lambda lt: (lt[1], lt[0]))


def _token_warning() -> str | None:
    """App auto-refreshes the token; banner only fires for real problems."""
    if not (settings.pushpress_email and settings.pushpress_password):
        return "PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD not configured."
    if not pushpress.active_token():
        return "PushPress login has not succeeded yet — check logs."
    return None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=False)
