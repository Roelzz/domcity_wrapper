from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    await _check_token_expiry()
    logger.info("Domcity Planner up on port {}", settings.port)
    yield
    scheduler.shutdown()


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
    prefill_lead: int | None = None,
):
    with DbSession(engine) as db:
        rules = db.exec(select(AutomationRule).order_by(AutomationRule.day_of_week)).all()
        attempts = db.exec(
            select(BookingAttempt).order_by(BookingAttempt.fired_at.desc()).limit(20)
        ).all()
    locations, categories = await _known_locations_and_categories()
    ctx = {
        "active": "automation",
        "rules": rules,
        "attempts": attempts,
        "next_fires": {
            r.id: scheduler.next_window_open(r).isoformat() for r in rules if r.enabled
        },
        "days": list(enumerate(DAYS_LONG)),
        "locations": locations,
        "categories": categories,
        "default_lead_time_hours": settings.default_lead_time_hours,
        "prefill": {
            "name": prefill_name or "",
            "location": prefill_location or "",
            "category": prefill_category or "",
            "day": prefill_day if prefill_day is not None else "",
            "time": prefill_time or "",
            "lead": prefill_lead or settings.default_lead_time_hours,
        },
        "token_warning": _token_warning(),
    }
    return templates.TemplateResponse(request, "automation.html", ctx)


@app.get("/automation/from-class/{slot_id}")
async def automation_from_class(slot_id: str):
    """Look up a class slot and redirect to /automation with the form pre-filled."""
    if not settings.pushpress_token:
        return RedirectResponse("/automation", status_code=303)
    today = datetime.now().date()
    # Lookback to start of current week so users can click slots earlier in the
    # visible week. Lookahead 14 days to cover next week + booking window.
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
        "prefill_lead": abs(slot.registration_start_offset_min or 0) // 60
        or settings.default_lead_time_hours,
    })
    return RedirectResponse(f"/automation?{qs}", status_code=303)


@app.post("/automation")
async def automation_create(
    name: str = Form(...),
    location: str = Form(...),
    class_category: str = Form(...),
    day_of_week: int = Form(...),
    time_of_day: str = Form(...),
    lead_time_hours: int = Form(None),
):
    try:
        hh, mm = (int(x) for x in time_of_day.split(":")[:2])
    except ValueError as e:
        raise HTTPException(400, f"Bad time: {e}") from e
    rule = AutomationRule(
        name=name.strip() or f"{DAYS_LONG[day_of_week]} {class_category}",
        location=location.strip(),
        class_category=class_category.strip(),
        day_of_week=day_of_week,
        time_of_day=time(hour=hh, minute=mm),
        lead_time_hours=lead_time_hours or settings.default_lead_time_hours,
        enabled=True,
    )
    with DbSession(engine) as db:
        db.add(rule)
        db.commit()
        db.refresh(rule)
    scheduler.schedule_rule(rule)
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
    scheduler.reschedule_all()
    return RedirectResponse("/automation", status_code=303)


@app.post("/automation/{rule_id}/delete")
async def automation_delete(rule_id: int):
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if rule:
            db.delete(rule)
            db.commit()
    scheduler.reschedule_all()
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
    if not settings.pushpress_token:
        return [], "PUSHPRESS_TOKEN not set — see .env.example for how to obtain it."
    try:
        items = await pushpress.list_schedule(start_d, end_d)
        return items, None
    except Exception as e:
        logger.exception("schedule fetch failed")
        return [], str(e)


async def _fetch_reservations():
    if not settings.pushpress_token:
        return [], "PUSHPRESS_TOKEN not set."
    try:
        return await pushpress.list_reservations(), None
    except Exception as e:
        logger.exception("reservations fetch failed")
        return [], str(e)


async def _booked_class_ids() -> set[str]:
    if not settings.pushpress_token:
        return set()
    try:
        return {r.class_id for r in await pushpress.list_reservations() if r.class_id}
    except Exception:
        return set()


async def _known_locations_and_categories() -> tuple[list[str], list[str]]:
    """Cache one week of schedule data on first request and derive unique
    locations + categories. Used by the automation form dropdowns."""
    if not settings.pushpress_token:
        return [], []
    today = datetime.now().date()
    try:
        items = await pushpress.list_schedule(today, today + timedelta(days=6))
    except Exception:
        logger.exception("locations/categories prefetch failed")
        return [], []
    locs = sorted({c.location for c in items if c.location})
    cats = sorted({c.category for c in items if c.category})
    return locs, cats


def _token_warning() -> str | None:
    if not settings.pushpress_token:
        return None
    exp = pushpress.token_expiry()
    if not exp:
        return None
    days_left = (exp - datetime.now(UTC)).days
    if days_left < 0:
        return "PushPress token expired — refresh PUSHPRESS_TOKEN."
    if days_left <= 7:
        return f"PushPress token expires in {days_left} days — refresh soon."
    return None


async def _check_token_expiry() -> None:
    msg = _token_warning()
    if msg:
        logger.warning(msg)
        await notify.send(f"⚠️ Domcity Planner: {msg}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=False)
