"""APScheduler integration. One in-process AsyncIOScheduler runs on FastAPI's
event loop. For each enabled AutomationRule we schedule a one-shot job at the
exact moment the booking window opens, then re-schedule the rule for next week
after the attempt completes."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlmodel import Session as DbSession
from sqlmodel import select

import notify
import pushpress
from models import AutomationRule, BookingAttempt, engine
from settings import settings

MAX_RETRIES = 10
RETRY_DELAY_SEC = 30

_scheduler: AsyncIOScheduler | None = None


def tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=tz())
    return _scheduler


def start() -> None:
    sch = get_scheduler()
    if not sch.running:
        sch.start()
        logger.info("Scheduler started (tz={})", settings.tz)
    reschedule_all()


def shutdown() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def next_class_datetime(rule: AutomationRule, now: datetime | None = None) -> datetime:
    """Compute the next datetime (tz-aware) for the class this rule targets."""
    now = now or datetime.now(tz())
    target = now.replace(
        hour=rule.time_of_day.hour,
        minute=rule.time_of_day.minute,
        second=0,
        microsecond=0,
    )
    days_ahead = (rule.day_of_week - now.weekday()) % 7
    target += timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return target


def next_window_open(rule: AutomationRule, now: datetime | None = None) -> datetime:
    class_dt = next_class_datetime(rule, now=now)
    return class_dt - timedelta(hours=rule.lead_time_hours)


def reschedule_all() -> None:
    sch = get_scheduler()
    # Wipe any rule-* jobs and re-add from DB
    for job in list(sch.get_jobs()):
        if job.id.startswith("rule-"):
            job.remove()
    with DbSession(engine) as db:
        rules = db.exec(select(AutomationRule).where(AutomationRule.enabled == True)).all()  # noqa: E712
    for rule in rules:
        schedule_rule(rule)


def schedule_rule(rule: AutomationRule) -> None:
    sch = get_scheduler()
    fire_at = next_window_open(rule)
    job_id = f"rule-{rule.id}"
    # If the moment already passed (within tolerance), push to next week
    if fire_at <= datetime.now(tz()):
        fire_at += timedelta(days=7)
    sch.add_job(
        booking_window_job,
        "date",
        run_date=fire_at,
        args=[rule.id, 0],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60,
    )
    logger.info(
        "Scheduled rule {} ('{}') for {}",
        rule.id,
        rule.class_name_pattern,
        fire_at.isoformat(),
    )


async def booking_window_job(rule_id: int, attempt: int) -> None:
    """One-shot job: try to book the next matching class. Retry on failure."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule or not rule.enabled:
            logger.info("Rule {} missing or disabled, skipping", rule_id)
            return

    class_dt = next_class_datetime(rule)
    target_label = f"{rule.class_name_pattern} @ {class_dt.isoformat()}"
    logger.info("Firing rule {} attempt {}: {}", rule_id, attempt + 1, target_label)

    try:
        session = await pushpress.login(settings.pushpress_email, settings.pushpress_password)
    except Exception as e:
        return await _handle_failure(rule, attempt, target_label, f"login failed: {e}")

    try:
        slots = await pushpress.list_schedule(
            session, class_dt - timedelta(hours=1), class_dt + timedelta(hours=1)
        )
        slot = _match_slot(slots, rule.class_name_pattern, class_dt)
        if not slot:
            return await _handle_failure(
                rule, attempt, target_label, "no matching class slot found"
            )
        result = await pushpress.book(session, slot.id)
        if result.ok:
            _record(rule.id, target_label, "success", result.message or "booked")
            await notify.send(f"✅ Booked: {target_label}")
            schedule_rule(rule)  # next week
        else:
            await _handle_failure(rule, attempt, target_label, result.message)
    finally:
        await session.aclose()


def _match_slot(slots, pattern: str, when: datetime):
    pat = pattern.lower()
    when_min = when - timedelta(minutes=30)
    when_max = when + timedelta(minutes=30)
    for s in slots:
        if pat in s.name.lower() and when_min <= s.start.astimezone(tz()) <= when_max:
            return s
    return None


async def _handle_failure(rule: AutomationRule, attempt: int, label: str, msg: str) -> None:
    logger.warning("Rule {} attempt {} failed: {}", rule.id, attempt + 1, msg)
    if attempt + 1 >= MAX_RETRIES:
        _record(rule.id, label, "failure", msg)
        await notify.send(f"❌ Failed: {label}\n{msg}")
        schedule_rule(rule)  # still try again next week
        return
    _record(rule.id, label, "retry", msg)
    run_at = datetime.now(tz()) + timedelta(seconds=RETRY_DELAY_SEC)
    get_scheduler().add_job(
        booking_window_job,
        "date",
        run_date=run_at,
        args=[rule.id, attempt + 1],
        id=f"rule-{rule.id}-retry-{attempt + 1}",
        replace_existing=True,
    )


def _record(rule_id: int, target: str, status: str, message: str) -> None:
    with DbSession(engine) as db:
        db.add(
            BookingAttempt(
                rule_id=rule_id, target_class=target, status=status, message=message[:1000]
            )
        )
        db.commit()
