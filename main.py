import sys
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlmodel import Session as DbSession
from sqlmodel import select

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    logger.info("Domcity Planner up on port {}", settings.port)
    yield
    scheduler.shutdown()


app = FastAPI(title="Domcity Planner", lifespan=lifespan)
app.add_middleware(AuthMiddleware)

BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


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
async def home(request: Request):
    return RedirectResponse("/schedule", status_code=303)


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request, date: str | None = None):
    start = _parse_date(date) or datetime.now().date()
    classes, err = await _fetch_schedule(start, start + timedelta(days=7))
    ctx = {
        "active": "schedule",
        "start": start,
        "prev_date": (start - timedelta(days=7)).isoformat(),
        "next_date": (start + timedelta(days=7)).isoformat(),
        "classes": classes,
        "error": err,
    }
    return templates.TemplateResponse(request, "schedule.html", ctx)


@app.post("/schedule/book/{slot_id}", response_class=HTMLResponse)
async def book_slot(request: Request, slot_id: str):
    try:
        session = await pushpress.login(settings.pushpress_email, settings.pushpress_password)
        result = await pushpress.book(session, slot_id)
        await session.aclose()
        if not result.ok:
            return HTMLResponse(f'<span class="error">Failed: {result.message}</span>', status_code=400)
        return HTMLResponse('<span class="success">Booked ✓</span>')
    except Exception as e:
        logger.exception("book failed")
        return HTMLResponse(f'<span class="error">{e}</span>', status_code=500)


# ---------- Reservations ----------
@app.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request):
    items, err = await _fetch_reservations()
    ctx = {"active": "reservations", "reservations": items, "error": err}
    return templates.TemplateResponse(request, "reservations.html", ctx)


@app.post("/reservations/{reservation_id}/cancel", response_class=HTMLResponse)
async def cancel_reservation(request: Request, reservation_id: str):
    try:
        session = await pushpress.login(settings.pushpress_email, settings.pushpress_password)
        ok = await pushpress.cancel(session, reservation_id)
        await session.aclose()
        if not ok:
            return HTMLResponse('<span class="error">Cancel failed</span>', status_code=400)
        return HTMLResponse("")  # row removed via hx-swap=delete
    except Exception as e:
        logger.exception("cancel failed")
        return HTMLResponse(f'<span class="error">{e}</span>', status_code=500)


# ---------- Automation ----------
@app.get("/automation", response_class=HTMLResponse)
async def automation_page(request: Request):
    with DbSession(engine) as db:
        rules = db.exec(select(AutomationRule).order_by(AutomationRule.day_of_week)).all()
        attempts = db.exec(
            select(BookingAttempt).order_by(BookingAttempt.fired_at.desc()).limit(20)
        ).all()
    ctx = {
        "active": "automation",
        "rules": rules,
        "attempts": attempts,
        "next_fires": {r.id: scheduler.next_window_open(r).isoformat() for r in rules if r.enabled},
    }
    return templates.TemplateResponse(request, "automation.html", ctx)


@app.post("/automation")
async def automation_create(
    class_name_pattern: str = Form(...),
    day_of_week: int = Form(...),
    time_of_day: str = Form(...),
    lead_time_hours: int = Form(24),
):
    try:
        hh, mm = (int(x) for x in time_of_day.split(":")[:2])
    except ValueError as e:
        raise HTTPException(400, f"Bad time: {e}") from e
    rule = AutomationRule(
        class_name_pattern=class_name_pattern.strip(),
        day_of_week=day_of_week,
        time_of_day=time(hour=hh, minute=mm),
        lead_time_hours=lead_time_hours,
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
def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


async def _fetch_schedule(start_d, end_d):
    if not settings.pushpress_email or not settings.pushpress_password:
        return [], "PushPress credentials not configured. Set PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD in .env."
    try:
        session = await pushpress.login(settings.pushpress_email, settings.pushpress_password)
        try:
            items = await pushpress.list_schedule(
                session, datetime.combine(start_d, time(0, 0)), datetime.combine(end_d, time(23, 59))
            )
        finally:
            await session.aclose()
        return items, None
    except Exception as e:
        logger.exception("schedule fetch failed")
        return [], str(e)


async def _fetch_reservations():
    if not settings.pushpress_email or not settings.pushpress_password:
        return [], "PushPress credentials not configured."
    try:
        session = await pushpress.login(settings.pushpress_email, settings.pushpress_password)
        try:
            items = await pushpress.list_reservations(session)
        finally:
            await session.aclose()
        return items, None
    except Exception as e:
        logger.exception("reservations fetch failed")
        return [], str(e)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=False)
    sys.exit(0)
