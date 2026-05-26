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

MAX_RETRIES = 5                 # transient retries only (network / 5xx)
RETRY_DELAY_SEC = 30
LOOKAHEAD_DAYS = 14
MIN_HOURS_BEFORE_CLASS = 1      # stop polling this close to class start
REMINDER_SCAN_INTERVAL_MIN = 15 # how often to refresh per-reservation reminders
REMINDER_DAY_HOUR = 8           # local hour for the same-day reminder
REMINDER_PRE_MIN = 30           # minutes before class start for the late reminder
MAX_CHAIN_DEPTH = 5             # hard cap on backup-rule chain length

# Substrings that mean the SLOT is full but could open up if someone cancels.
# These rules switch into 12-hour polling mode instead of fast-retrying.
CLASS_FULL_KEYWORDS = (
    "class is full",
    "fully booked",
    "no spots",
    "no spot available",
    "no available spots",
    "sold out",
    "no longer available",
    "capacity",          # "at capacity", "capacity reached"
    "no space",
    "waitlist",          # if returned as an error, treat as full
)

# Substrings about the USER's account — retrying won't help no matter how long.
# Kept tight on purpose: a bare "expired" matches "Token expired" (transient!)
# and a bare "cancelled" matches a gym-cancelled class (handled by the
# class-vanished branch). Phrases must be specific enough to mean the user.
USER_TERMINAL_KEYWORDS = (
    "exceeded",
    "registration cap",
    "already reserved",
    "no permission",
    "not authorized",
    "no active membership",
    "subscription is not active",
    "not allowed",
)


def _is_class_full(msg: str) -> bool:
    low = (msg or "").lower()
    return any(kw in low for kw in CLASS_FULL_KEYWORDS)


def _is_user_terminal(msg: str) -> bool:
    low = (msg or "").lower()
    return any(kw in low for kw in USER_TERMINAL_KEYWORDS)

_scheduler: AsyncIOScheduler | None = None

# Defensive loop guard: tracks the last time booking_window_job actually ran
# the booking logic for each rule. If the same rule fires again within
# LOOP_GUARD_WINDOW_SEC, we abort that fire instead of re-attempting — this
# stops a runaway scheduling loop from spamming Telegram or PushPress.
_last_fire_at: dict[int, datetime] = {}
LOOP_GUARD_WINDOW_SEC = 30


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


def schedule_daily_digest() -> None:
    """Daily 09:00 cron that flushes the digest buffer. notify.flush_digest()
    is a no-op when the buffer is empty, so the Telegram only fires on days
    that actually had something to report."""
    sch = get_scheduler()
    sch.add_job(
        notify.flush_digest,
        "cron",
        hour=9,
        minute=0,
        id="daily-digest",
        replace_existing=True,
    )
    logger.info("Daily digest cron scheduled at 09:00 {}", settings.tz)


def schedule_reminder_scan() -> None:
    """Periodic job that keeps per-reservation reminders up to date."""
    sch = get_scheduler()
    sch.add_job(
        reminder_scan_job,
        "interval",
        minutes=REMINDER_SCAN_INTERVAL_MIN,
        id="reminder-scan",
        replace_existing=True,
        next_run_time=datetime.now(tz()) + timedelta(seconds=15),
    )
    logger.info("Reminder scan scheduled every {} min", REMINDER_SCAN_INTERVAL_MIN)


async def reminder_scan_job() -> None:
    """Fetch upcoming reservations and (re-)schedule two reminders per booking:
    one at 08:00 local on the class day and one 30 minutes before class start.
    Idempotent — job IDs are deterministic + replace_existing."""
    try:
        reservations = await pushpress.list_reservations()
    except Exception as e:
        logger.warning("Reminder scan: list_reservations failed: {}", e)
        return
    sch = get_scheduler()
    now = datetime.now(tz())
    scheduled = 0
    for r in reservations:
        if not r.cancellable:
            continue
        start_local = r.start.astimezone(tz())
        if start_local <= now:
            continue

        day_reminder = start_local.replace(
            hour=REMINDER_DAY_HOUR, minute=0, second=0, microsecond=0
        )
        if now < day_reminder < start_local:
            sch.add_job(
                reminder_day_job,
                "date",
                run_date=day_reminder,
                args=[r.id],
                id=f"reminder-{r.id}-day",
                replace_existing=True,
                misfire_grace_time=60 * 10,
            )
            scheduled += 1

        pre = start_local - timedelta(minutes=REMINDER_PRE_MIN)
        if pre > now:
            sch.add_job(
                reminder_pre_job,
                "date",
                run_date=pre,
                args=[r.id],
                id=f"reminder-{r.id}-pre",
                replace_existing=True,
                misfire_grace_time=60 * 5,
            )
            scheduled += 1
    logger.debug(
        "Reminder scan: ensured {} reminders across {} reservations",
        scheduled, len(reservations),
    )


async def reminder_day_job(reservation_id: str) -> None:
    r = await _active_reservation(reservation_id)
    if not r:
        return
    await notify.send(_reminder_message("📅 Today", r))


async def reminder_pre_job(reservation_id: str) -> None:
    r = await _active_reservation(reservation_id)
    if not r:
        return
    await notify.send(_reminder_message(f"⏰ Starts in {REMINDER_PRE_MIN} min", r))


def _reminder_message(prefix: str, r) -> str:
    start_local = r.start.astimezone(tz())
    lines = [f"{prefix} at {start_local.strftime('%H:%M')}: {r.class_name}"]
    if r.location:
        lines.append(f"📍 {r.location}")
    if r.instructor:
        lines.append(f"👤 {r.instructor}")
    return "\n".join(lines)


async def _active_reservation(reservation_id: str):
    try:
        reservations = await pushpress.list_reservations()
    except Exception:
        return None
    return next(
        (r for r in reservations if r.id == reservation_id and r.cancellable), None
    )


async def token_refresh_job() -> None:
    """Daily 03:00 cron. Successful refreshes are logged silently. Failures
    go to the daily digest, not immediate Telegram."""
    exp = pushpress.token_expiry()
    if not exp:
        logger.warning("Token refresh job: no active token, forcing login")
        try:
            await pushpress.force_refresh()
        except Exception as e:
            logger.error("Token refresh failed: {}", e)
            notify.queue_for_digest(f"❌ token refresh failed: {e}")
        return
    days_left = (exp - datetime.now(tz()).astimezone(exp.tzinfo)).days
    if days_left > 7:
        logger.info("Token has {} days left, no refresh needed", days_left)
        return
    logger.info("Token has {} days left, refreshing", days_left)
    try:
        await pushpress.force_refresh()
        logger.info("Token refreshed (was {}d from expiry)", days_left)
    except Exception as e:
        logger.error("Token refresh failed: {}", e)
        notify.queue_for_digest(f"❌ token refresh failed (was {days_left}d from expiry): {e}")


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
        local = s.start.astimezone(tz())
        if local.weekday() != rule.day_of_week:
            continue
        if local.time().hour != rule.time_of_day.hour:
            continue
        if local.time().minute != rule.time_of_day.minute:
            continue
        if local <= after:
            continue
        # Skip anything on or before the rule's paused_until date.
        if rule.paused_until and local.date() <= rule.paused_until:
            continue
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
        rules = db.exec(
            select(AutomationRule).where(
                AutomationRule.enabled == True,  # noqa: E712
                AutomationRule.backup_only == False,  # noqa: E712
            )
        ).all()
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
        args=[rule.id, 0, slot.id],
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


async def booking_window_job(
    rule_id: int,
    attempt: int,
    calendar_item_uuid: str,
    chained_from: set[int] | None = None,
    manual: bool = False,
) -> None:
    """One-shot job: try to book the specific class this rule was queued for.
    Branches:
      - success → log + notify + schedule next week's class
      - class full at window-open → poll for cancellations, OR (when rule has
        a backup_rule_id) chain straight to the backup
      - user-terminal error (cap exceeded etc) → maybe chain + give up
      - transient → retry every 30s up to MAX_RETRIES, then maybe chain

    `calendar_item_uuid` is the slot _schedule_after / schedule_rule queued
    this job for — it's the source of truth. We do NOT re-resolve "the next
    matching slot" here, because that lookup is time-sensitive (a slot from
    this week can re-appear if the job fires before its class starts) and
    used to cause infinite loops re-booking the same idempotent reservation.

    `chained_from` carries the rule IDs already tried in the current backup
    chain — used for cycle detection. `manual` bypasses the loop guard so a
    user-initiated fire isn't suppressed by a recent scheduled fire of the
    same rule.
    """
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule or not rule.enabled:
            logger.info("Rule {} missing or disabled, skipping", rule_id)
            return

    now = datetime.now(tz())
    if not manual:
        last = _last_fire_at.get(rule_id)
        if last is not None and (now - last).total_seconds() < LOOP_GUARD_WINDOW_SEC:
            elapsed = (now - last).total_seconds()
            logger.error(
                "Rule {} firing too fast (last {:.1f}s ago), aborting to break loop",
                rule_id, elapsed,
            )
            notify.queue_for_digest(
                f"⚠️ rule {rule_id} ('{rule.name}') loop-guarded ({elapsed:.0f}s)"
            )
            return
        _last_fire_at[rule_id] = now

    slot = await _lookup_slot(calendar_item_uuid)
    if slot is None:
        # Slot vanished between scheduling and firing (gym cancelled / past).
        label = f"{rule.name} (cal {calendar_item_uuid[:20]}…)"
        logger.warning(
            "Firing rule {} attempt {}: target slot {} no longer on schedule",
            rule_id, attempt + 1, calendar_item_uuid,
        )
        await _give_up_this_week(
            rule, None, label,
            "target class disappeared before booking",
            chained_from=chained_from, notify_immediate=False,
        )
        return

    class_start = slot.start.astimezone(tz())
    target_label = f"{rule.name} — {class_start.strftime('%a %d %b %H:%M')}"
    logger.info("Firing rule {} attempt {}: {}", rule_id, attempt + 1, target_label)

    # Class full at window-open: poll for cancellations, UNLESS the rule has
    # a backup configured — in that case the chain takes over immediately
    # and polling is skipped (the user prefers a backup booking now over
    # waiting for a cancellation).
    if (slot.spots_available or 0) <= 0:
        reason = (
            f"class full at window open "
            f"({slot.spots_available}/{slot.spots_total})"
        )
        if rule.backup_rule_id is not None:
            await _give_up_this_week(
                rule, slot, target_label, reason,
                chained_from=chained_from, notify_immediate=False,
            )
            return
        return await _start_polling(rule, slot, target_label, reason)

    try:
        result = await pushpress.book(slot.id)
    except Exception as e:
        return await _handle_failure(
            rule, attempt, target_label, f"book call raised: {e}",
            slot=slot, chained_from=chained_from,
        )
    if result.ok:
        _record(rule.id, target_label, "success", result.message or "booked")
        await notify.send(f"✅ Booked: {target_label}")
        # Bump the reminder scan so reminders for THIS new reservation land
        # immediately instead of waiting up to 15 min for the next interval.
        await reminder_scan_job()
        # Re-schedule for the FOLLOWING week (look past the slot we just booked).
        await _schedule_after(rule, class_start + timedelta(minutes=1))
    else:
        await _handle_failure(
            rule, attempt, target_label, result.message,
            slot=slot, chained_from=chained_from,
        )


async def _lookup_slot(calendar_item_uuid: str):
    """Fetch the specific slot we're about to book by uuid. Returns the
    ClassSlot or None if it's not in the upcoming schedule."""
    today = datetime.now(tz()).date()
    try:
        slots = await pushpress.list_schedule(today, today + timedelta(days=LOOKAHEAD_DAYS))
    except Exception as e:
        logger.warning("_lookup_slot fetch failed for {}: {}", calendar_item_uuid, e)
        return None
    return next((s for s in slots if s.id == calendar_item_uuid), None)


async def class_full_poll_job(rule_id: int, calendar_item_uuid: str) -> None:
    """Periodic check: did a spot open up for the full class this rule wants?
    Fires every POLL_INTERVAL_HOURS hours, stops MIN_HOURS_BEFORE_CLASS before
    class start."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule or not rule.enabled:
            logger.info("Rule {} missing or disabled, stopping poll", rule_id)
            return

    today = datetime.now(tz()).date()
    try:
        classes = await pushpress.list_schedule(today, today + timedelta(days=LOOKAHEAD_DAYS))
    except Exception as e:
        logger.warning("Poll fetch failed for rule {}: {}", rule_id, e)
        # Try again next interval against a stale slot dummy
        await _schedule_poll_retry(rule_id, calendar_item_uuid)
        return

    slot = next((c for c in classes if c.id == calendar_item_uuid), None)
    if slot is None:
        # Class has vanished — gym cancelled it, or it scrolled past the
        # lookahead window. Either way, move on (and try backup if set).
        label = f"{rule.name} (cal {calendar_item_uuid[:20]}…)"
        await _give_up_this_week(
            rule, None, label, "class no longer on schedule",
            chained_from=None,
        )
        return

    class_start = slot.start.astimezone(tz())
    now = datetime.now(tz())
    label = f"{rule.name} — {class_start.strftime('%a %d %b %H:%M')}"

    if class_start <= now:
        await _give_up_this_week(
            rule, slot, label, "class started, never got a spot",
            chained_from=None,
        )
        return

    spots = slot.spots_available or 0
    if spots <= 0:
        # Still full — log + reschedule the next poll.
        _record(rule.id, label, "polling", f"still full ({spots}/{slot.spots_total})")
        logger.info("Rule {} poll: still full ({}/{})", rule_id, spots, slot.spots_total)
        return await _schedule_poll(rule, slot)

    # Spot opened up — race to book it.
    logger.info("Rule {} poll: spot opened ({}/{}), attempting book", rule_id, spots, slot.spots_total)
    try:
        result = await pushpress.book(slot.id)
    except Exception as e:
        logger.warning("Book during poll raised: {}", e)
        return await _schedule_poll(rule, slot)

    if result.ok:
        _record(rule.id, label, "success", "booked from poll")
        await notify.send(f"✅ Booked from poll: {label}")
        await reminder_scan_job()
        await _schedule_after(rule, class_start + timedelta(minutes=1))
        return

    # Someone took it first / different error
    if _is_user_terminal(result.message):
        await _give_up_this_week(
            rule, slot, label, f"terminal during poll: {result.message}",
            chained_from=None, notify_immediate=False,
        )
        return
    # Race lost, or some other recoverable error — keep polling
    _record(rule.id, label, "polling", f"poll race lost: {result.message}")
    await _schedule_poll(rule, slot)


async def _start_polling(rule: AutomationRule, slot, label: str, reason: str) -> None:
    _record(rule.id, label, "polling", reason)
    # Polling-started is informational — goes to the daily digest, not an
    # immediate Telegram. The user only needs to know if the poll ends in
    # a final ✅ or ❌, which still fires immediately.
    notify.queue_for_digest(
        f"⏳ {rule.name}: class full at window-open, started polling"
    )
    await _schedule_poll(rule, slot)


def _poll_interval_for(time_until_class: timedelta) -> timedelta:
    """Adaptive poll cadence — tighter as the class approaches, because the
    bulk of last-minute cancellations cluster in the final 24h.

      > 48h:  12h
      24h–48h: 4h
      6h–24h:  1h
      1h–6h:   15min
      ≤1h:     handled by the deadline check, not this function
    """
    secs = time_until_class.total_seconds()
    if secs > 48 * 3600:
        return timedelta(hours=12)
    if secs > 24 * 3600:
        return timedelta(hours=4)
    if secs > 6 * 3600:
        return timedelta(hours=1)
    return timedelta(minutes=15)


async def _schedule_poll(rule: AutomationRule, slot) -> None:
    """Schedule the next poll for this rule's target class, or give up if we're
    too close to class start to keep trying. Note polling is only entered for
    rules without a backup_rule_id — rules with backups skip polling and chain
    immediately — so this give-up path doesn't need to thread chain context."""
    sch = get_scheduler()
    now = datetime.now(tz())
    class_start = slot.start.astimezone(tz())
    next_poll = now + _poll_interval_for(class_start - now)
    deadline = class_start - timedelta(hours=MIN_HOURS_BEFORE_CLASS)
    if next_poll > deadline:
        # Out of time. Final failure for this week.
        label = f"{rule.name} — {class_start.strftime('%a %d %b %H:%M')}"
        await _give_up_this_week(
            rule, slot, label, "class stayed full through booking window",
            chained_from=None,
        )
        return
    sch.add_job(
        class_full_poll_job,
        "date",
        run_date=next_poll,
        args=[rule.id, slot.id],
        id=f"rule-{rule.id}-poll",
        replace_existing=True,
        misfire_grace_time=60 * 30,
    )
    logger.info(
        "Polling rule {} ('{}') again at {} for class {}",
        rule.id, rule.name, next_poll.isoformat(), slot.id,
    )


async def _schedule_poll_retry(rule_id: int, calendar_item_uuid: str) -> None:
    """Short retry when the periodic fetch itself errored — try again in 1h."""
    sch = get_scheduler()
    sch.add_job(
        class_full_poll_job,
        "date",
        run_date=datetime.now(tz()) + timedelta(hours=1),
        args=[rule_id, calendar_item_uuid],
        id=f"rule-{rule_id}-poll",
        replace_existing=True,
    )


async def _schedule_after(rule: AutomationRule, after: datetime) -> None:
    # Backup-only rules don't run on their own schedule — only when chained
    # from a parent. Don't queue an APScheduler job for them.
    if rule.backup_only:
        return
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
        args=[rule.id, 0, slot.id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60,
    )
    logger.info(
        "Re-scheduled rule {} ('{}') for {} (next class {} at {})",
        rule.id, rule.name, fire_at.isoformat(), slot.id, slot.start.isoformat(),
    )


async def _advance_past_slot(rule: AutomationRule, slot) -> None:
    """Reschedule the rule for the class AFTER `slot`. Used when the current
    slot is unrecoverable (terminal failure, retries exhausted). Using
    slot.start + 1min as the cutoff guarantees find_next_matching_slot
    returns a DIFFERENT slot, breaking any retry loop. Falls back to a
    7-day skip if slot is None (no anchor to advance past)."""
    if slot is not None:
        after = slot.start.astimezone(tz()) + timedelta(minutes=1)
    else:
        after = next_class_datetime(rule, datetime.now(tz())) + timedelta(days=1)
    await _schedule_after(rule, after)


async def _handle_failure(
    rule: AutomationRule,
    attempt: int,
    label: str,
    msg: str,
    slot=None,
    chained_from: set[int] | None = None,
) -> None:
    logger.warning("Rule {} attempt {} failed: {}", rule.id, attempt + 1, msg)

    # Class-full mid-flow → poll for cancellations, UNLESS a backup is
    # configured (in which case skip polling and chain immediately).
    if slot is not None and _is_class_full(msg):
        if rule.backup_rule_id is not None:
            await _give_up_this_week(
                rule, slot, label, msg,
                chained_from=chained_from, notify_immediate=False,
            )
            return
        await _start_polling(rule, slot, label, msg)
        return

    # User-side terminal → give up on THIS slot (and chain to backup if set).
    # Quiet path: digest instead of immediate Telegram — the rule already
    # gave up, nothing for the user to act on right now.
    if _is_user_terminal(msg):
        await _give_up_this_week(
            rule, slot, label, f"terminal: {msg}",
            chained_from=chained_from, notify_immediate=False,
        )
        return

    # Transient → retry every 30s up to MAX_RETRIES, then give up + maybe chain.
    if attempt + 1 >= MAX_RETRIES:
        await _give_up_this_week(
            rule, slot, label,
            f"gave up after {MAX_RETRIES} retries: {msg}",
            chained_from=chained_from, notify_immediate=False,
        )
        return
    _record(rule.id, label, "retry", msg)
    run_at = datetime.now(tz()) + timedelta(seconds=RETRY_DELAY_SEC)
    # Retry the SAME slot — pass slot.id forward. If slot is somehow None at
    # this point, there's no slot to retry.
    if slot is None:
        return
    retry_kwargs = {"chained_from": chained_from} if chained_from else None
    get_scheduler().add_job(
        booking_window_job,
        "date",
        run_date=run_at,
        args=[rule.id, attempt + 1, slot.id],
        kwargs=retry_kwargs,
        id=f"rule-{rule.id}-retry-{attempt + 1}",
        replace_existing=True,
    )


async def _give_up_this_week(
    rule: AutomationRule,
    slot,
    target_label: str,
    reason: str,
    *,
    chained_from: set[int] | None = None,
    notify_immediate: bool = True,
) -> None:
    """Single exit point for 'this slot can't be booked this week'. Records
    the failure, fires the backup chain if one is configured, and re-schedules
    the primary for next week (backup-only rules opt out of re-scheduling via
    _schedule_after / _advance_past_slot).

    `chained_from` carries the visited set so a chained give-up doesn't loop.
    `notify_immediate=False` routes the user-facing message to the daily digest
    instead of an immediate Telegram — for terminal/quiet failures where the
    user has nothing to act on right now."""
    _record(rule.id, target_label, "failure", reason)
    visited = (chained_from or set()) | {rule.id}
    if slot is not None:
        target_start = slot.start.astimezone(tz())
    else:
        target_start = next_class_datetime(rule, datetime.now(tz()))

    chained = await _chain_to_backup(rule, target_start, visited, reason)
    if not chained:
        # No backup picked it up — surface to the user.
        msg = f"❌ {rule.name}: {reason}"
        if notify_immediate:
            await notify.send(msg)
        else:
            notify.queue_for_digest(msg)

    if slot is not None:
        await _advance_past_slot(rule, slot)
    else:
        await _schedule_after(rule, target_start + timedelta(days=1))


async def _chain_to_backup(
    parent: AutomationRule,
    target_class_start: datetime,
    visited: set[int],
    reason: str,
) -> bool:
    """Walk the backup chain starting at `parent.backup_rule_id`. Returns True
    if a backup successfully fired its booking_window_job, False if there's no
    backup configured, the chain has no fireable link, or the depth cap is hit.

    `visited` is the set of rule IDs already tried in this chain (including
    `parent.id`). Cycle detection rejects any backup whose ID is already in
    `visited`. The depth cap is MAX_CHAIN_DEPTH, comparing against `len(visited)`."""
    if parent.backup_rule_id is None:
        return False
    if len(visited) >= MAX_CHAIN_DEPTH:
        logger.warning(
            "Backup chain depth cap ({}) hit at rule {}; not chaining further",
            MAX_CHAIN_DEPTH, parent.id,
        )
        notify.queue_for_digest(
            f"⚠️ {parent.name}: backup chain hit depth cap"
        )
        return False
    with DbSession(engine) as db:
        backup = db.get(AutomationRule, parent.backup_rule_id)
    if backup is None:
        logger.warning(
            "Rule {} has backup_rule_id={} but that rule doesn't exist",
            parent.id, parent.backup_rule_id,
        )
        return False
    if backup.id in visited:
        logger.error(
            "Backup chain cycle: rule {} already visited {}", backup.id, visited
        )
        return False
    if not backup.enabled:
        logger.info(
            "Backup rule {} ('{}') is disabled, skipping to its own backup",
            backup.id, backup.name,
        )
        return await _chain_to_backup(
            backup, target_class_start, visited | {backup.id}, reason
        )
    # Find a matching slot for the backup around the failed target's week.
    week_anchor = target_class_start - timedelta(days=2)
    slot, _ = await find_next_matching_slot(backup, after=week_anchor)
    if slot is None:
        logger.info(
            "Backup rule {} ('{}'): no matching slot near {}, trying next in chain",
            backup.id, backup.name, target_class_start.isoformat(),
        )
        return await _chain_to_backup(
            backup, target_class_start, visited | {backup.id}, reason
        )
    logger.info(
        "Chaining rule {} → backup {} ('{}') for class {} (reason: {})",
        parent.id, backup.id, backup.name, slot.id, reason,
    )
    notify.queue_for_digest(
        f"↦ {parent.name} failed, trying backup {backup.name}"
    )
    await booking_window_job(
        backup.id, 0, slot.id,
        chained_from=visited,
    )
    return True


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
