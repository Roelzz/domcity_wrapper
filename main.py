from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlmodel import Session as DbSession
from sqlmodel import select

import notify
import pushpress
import scheduler
from auth import COOKIE_NAME, AuthMiddleware, make_session_token
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    await _prime_token()
    await scheduler.reschedule_all()
    scheduler.schedule_token_refresh()
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
        logger.error("Initial token load failed: {}", e)
        await notify.send(f"❌ Domcity Planner: PushPress login failed at startup\n{e}")


app = FastAPI(title="Domcity Planner", lifespan=lifespan)
app.add_middleware(AuthMiddleware)

BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ---------- Health ----------
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---------- Auth ----------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(password: str = Form(...)):
    if password != settings.app_password:
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
        "total_count": len(filtered),
        "unfiltered_count": len(all_classes),
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
@app.get("/automation", response_class=HTMLResponse)
async def automation_page(
    request: Request,
    prefill_name: str | None = None,
    prefill_location: str | None = None,
    prefill_category: str | None = None,
    prefill_day: int | None = None,
    prefill_time: str | None = None,
):
    with DbSession(engine) as db:
        rules = db.exec(select(AutomationRule).order_by(AutomationRule.day_of_week)).all()
        attempts = db.exec(
            select(BookingAttempt).order_by(BookingAttempt.fired_at.desc()).limit(20)
        ).all()

    classes = await _fetch_classes_for_automation()
    categories = sorted({c.category for c in classes if c.category})

    next_fires: dict[int, str] = {}
    now = datetime.now(scheduler.tz())
    for r in rules:
        if not r.enabled:
            continue
        fire = _compute_next_fire_from(classes, r, now)
        next_fires[r.id] = fire.isoformat() if fire else "—"

    # Compute the cascading day/time block from prefill (or empty)
    selected_slot = ""
    if prefill_location and prefill_time:
        selected_slot = f"{prefill_location}{SLOT_SEPARATOR}{prefill_time}"
    day_time_ctx = _build_day_time_ctx(
        classes,
        category=prefill_category or "",
        dow=prefill_day if prefill_day is not None else None,
        selected_slot=selected_slot,
    )

    ctx = {
        "active": "automation",
        "rules": rules,
        "attempts": attempts,
        "next_fires": next_fires,
        "days": list(enumerate(DAYS_LONG)),
        "categories": categories,
        "day_time": day_time_ctx,
        "prefill": {
            "name": prefill_name or "",
            "category": prefill_category or "",
        },
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
        if local > now:
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
    await scheduler.schedule_rule(rule)
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
    await scheduler.reschedule_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/delete")
async def automation_delete(rule_id: int):
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if rule:
            db.delete(rule)
            db.commit()
    await scheduler.reschedule_all()
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
