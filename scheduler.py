"""APScheduler integration. One in-process AsyncIOScheduler runs on FastAPI's
event loop. For each enabled AutomationRule we look up the next matching class,
read its registrationStartOffset to find the exact moment its booking window
opens, schedule a one-shot job at that timestamp. After every booking attempt
the rule is re-scheduled against the following week's class."""

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
LOOKAHEAD_DAYS = 14  # how far ahead to search for a class matching a rule

# Substrings in a PushPress error message that signal there's no point retrying:
# the booking will keep failing for the same reason. Match case-insensitively.
TERMINAL_ERROR_KEYWORDS = (
    "exceeded",          # "Exceeded registration cap"
    "registration cap",
    "already",           # "Already reserved"
    "no permission",
    "not authorized",
    "membership",        # "No active membership"
    "subscription",      # "Subscription is not active"
    "expired",
    "cancelled",
    "not allowed",
)


def _is_terminal_error(msg: str) -> bool:
    low = (msg or "").lower()
    return any(kw in low for kw in TERMINAL_ERROR_KEYWORDS)

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


def shutdown() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def schedule_token_refresh() -> None:
    """Daily 03:00 cron: re-login if the cached token expires in < 7 days."""
    sch = get_scheduler()
    sch.add_job(
        token_refresh_job,
        "cron",
        hour=3,
        minute=0,
        id="token-refresh",
        replace_existing=True,
    )
    logger.info("Token refresh cron scheduled daily at 03:00 {}", settings.tz)


async def token_refresh_job() -> None:
    exp = pushpress.token_expiry()
    if not exp:
        logger.warning("Token refresh job: no active token, forcing login")
        try:
            await pushpress.force_refresh()
        except Exception as e:
            logger.error("Token refresh failed: {}", e)
            await notify.send(f"❌ Domcity Planner: token refresh failed\n{e}")
        return
    days_left = (exp - datetime.now(tz()).astimezone(exp.tzinfo)).days
    if days_left > 7:
        logger.info("Token has {} days left, no refresh needed", days_left)
        return
    logger.info("Token has {} days left, refreshing", days_left)
    try:
        await pushpress.force_refresh()
        await notify.send(f"🔑 Domcity Planner: refreshed PushPress token (was {days_left}d from expiry)")
    except Exception as e:
        logger.error("Token refresh failed: {}", e)
        await notify.send(f"❌ Domcity Planner: token refresh failed\n{e}")


def next_class_datetime(rule: AutomationRule, now: datetime | None = None) -> datetime:
    """The next tz-aware datetime when a class matching this rule should occur,
    based only on day_of_week + time_of_day. Used as a hint when looking up the
    real class slot. Always strictly in the future."""
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


async def find_next_matching_slot(
    rule: AutomationRule, after: datetime | None = None
):
    """Look up the next class in PushPress matching this rule. Returns
    (ClassSlot, target_datetime) or (None, target_datetime_hint)."""
    after = after or datetime.now(tz())
    hint = next_class_datetime(rule, now=after)
    start = hint.date() - timedelta(days=1)
    end = hint.date() + timedelta(days=LOOKAHEAD_DAYS)
    try:
        slots = await pushpress.list_schedule(start, end)
    except Exception as e:
        logger.warning("schedule fetch for rule {} failed: {}", rule.id, e)
        return None, hint
    candidates = []
    for s in slots:
        if rule.location and s.location.lower() != rule.location.lower():
            continue
        if rule.class_category and s.category.lower() != rule.class_category.lower():
            continue
        if s.start.astimezone(tz()).weekday() != rule.day_of_week:
            continue
        if s.start.astimezone(tz()).time().hour != rule.time_of_day.hour:
            continue
        if s.start.astimezone(tz()).time().minute != rule.time_of_day.minute:
            continue
        if s.start.astimezone(tz()) > after:
            candidates.append(s)
    candidates.sort(key=lambda s: s.start)
    return (candidates[0] if candidates else None), hint


def window_open_time(slot) -> datetime:
    """When does the booking window open for this slot? Uses the slot's own
    registrationStartOffset (negative minutes; e.g. -20160 for 14 days)."""
    offset_min = slot.registration_start_offset_min or 0
    return slot.start.astimezone(tz()) + timedelta(minutes=offset_min)


async def reschedule_all() -> None:
    sch = get_scheduler()
    for job in list(sch.get_jobs()):
        if job.id.startswith("rule-"):
            job.remove()
    with DbSession(engine) as db:
        rules = db.exec(select(AutomationRule).where(AutomationRule.enabled == True)).all()  # noqa: E712
    for rule in rules:
        await schedule_rule(rule)


async def schedule_rule(rule: AutomationRule) -> None:
    """Find the next matching class, schedule a job at its booking-window-open
    time. If the window is already open, fire in 2 seconds."""
    sch = get_scheduler()
    job_id = f"rule-{rule.id}"
    slot, hint = await find_next_matching_slot(rule)
    if slot is None:
        logger.warning(
            "Rule {} ('{}'): no matching class in next {} days near {}; not scheduled",
            rule.id, rule.name, LOOKAHEAD_DAYS, hint.isoformat(),
        )
        return
    fire_at = window_open_time(slot)
    now = datetime.now(tz())
    if fire_at <= now:
        # window already open — fire ASAP
        fire_at = now + timedelta(seconds=2)
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
        "Scheduled rule {} ('{}') for {} (class {} at {})",
        rule.id, rule.name, fire_at.isoformat(), slot.id, slot.start.isoformat(),
    )


async def next_window_open_async(rule: AutomationRule) -> datetime | None:
    """For UI display: what's the next planned fire time?"""
    slot, _ = await find_next_matching_slot(rule)
    if not slot:
        return None
    fire = window_open_time(slot)
    return max(fire, datetime.now(tz()))


async def booking_window_job(rule_id: int, attempt: int) -> None:
    """One-shot job: try to book the next matching class. Retry on failure."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule or not rule.enabled:
            logger.info("Rule {} missing or disabled, skipping", rule_id)
            return

    slot, hint = await find_next_matching_slot(rule)
    target_label = f"{rule.name} — {hint.strftime('%a %d %b %H:%M')}"
    logger.info("Firing rule {} attempt {}: {}", rule_id, attempt + 1, target_label)

    if not slot:
        return await _handle_failure(rule, attempt, target_label, "no matching class found")

    try:
        result = await pushpress.book(slot.id)
    except Exception as e:
        return await _handle_failure(rule, attempt, target_label, f"book call raised: {e}")
    if result.ok:
        _record(rule.id, target_label, "success", result.message or "booked")
        await notify.send(f"✅ Booked: {target_label}")
        # Re-schedule for the FOLLOWING week (look past the slot we just booked).
        await _schedule_after(rule, slot.start.astimezone(tz()) + timedelta(minutes=1))
    else:
        await _handle_failure(rule, attempt, target_label, result.message)


async def _schedule_after(rule: AutomationRule, after: datetime) -> None:
    sch = get_scheduler()
    job_id = f"rule-{rule.id}"
    slot, hint = await find_next_matching_slot(rule, after=after)
    if not slot:
        logger.warning(
            "Rule {} ('{}'): no NEXT class found after {}", rule.id, rule.name, after.isoformat()
        )
        return
    fire_at = window_open_time(slot)
    now = datetime.now(tz())
    if fire_at <= now:
        fire_at = now + timedelta(seconds=2)
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
        "Re-scheduled rule {} ('{}') for {} (next class {} at {})",
        rule.id, rule.name, fire_at.isoformat(), slot.id, slot.start.isoformat(),
    )


async def _handle_failure(rule: AutomationRule, attempt: int, label: str, msg: str) -> None:
    logger.warning("Rule {} attempt {} failed: {}", rule.id, attempt + 1, msg)
    terminal = _is_terminal_error(msg)
    if terminal or attempt + 1 >= MAX_RETRIES:
        status = "failure" if terminal else "failure"
        reason = f"terminal: {msg}" if terminal else msg
        _record(rule.id, label, status, reason)
        await notify.send(f"❌ Failed ({'terminal' if terminal else 'gave up'}): {label}\n{msg}")
        # Try again next week
        await _schedule_after(rule, datetime.now(tz()) + timedelta(hours=1))
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


# Kept for backward compat / tests. Used to be sync.
def _match_slot(slots, rule: AutomationRule, when: datetime):
    when_min = when - timedelta(minutes=30)
    when_max = when + timedelta(minutes=30)
    cat = rule.class_category.lower()
    loc = rule.location.lower()
    for s in slots:
        if loc and s.location.lower() != loc:
            continue
        if cat and s.category.lower() != cat:
            continue
        if when_min <= s.start.astimezone(tz()) <= when_max:
            return s
    return None
