"""APScheduler integration. One in-process AsyncIOScheduler runs on FastAPI's
event loop. For each enabled AutomationRule we look up the next matching class,
read its registrationStartOffset to find the exact moment its booking window
opens, schedule a one-shot job at that timestamp. After every booking attempt
the rule is re-scheduled against the following week's class."""

from __future__ import annotations

from datetime import date, datetime, timedelta
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
REMINDER_SCAN_INTERVAL_MIN = 15 # how often to refresh per-reservation reminders
REMINDER_DAY_HOUR = 8           # local hour for the same-day reminder
REMINDER_PRE_MIN = 30           # minutes before class start for the late reminder
MAX_CHAIN_DEPTH = 5             # hard cap on backup-rule chain length
WARNING_LEAD_DAYS = 3           # warn this many days before period_end if overbooked

# Substrings about the USER's account — retrying won't help no matter how long.
# Kept tight on purpose: a bare "expired" matches "Token expired" (transient!)
# and a bare "cancelled" matches a gym-cancelled class (handled by the
# class-vanished branch). Phrases must be specific enough to mean the user.
# Any failure message that matches is treated as terminal for this slot;
# the backup chain (if any) covers the cancellation-watch role that the
# removed polling mechanism used to handle. Class-full keywords are included
# so a mid-flow full-class response (rare race with the spot_check) doesn't
# burn 5 retries.
USER_TERMINAL_KEYWORDS = (
    "exceeded",
    "registration cap",
    "already reserved",
    "no permission",
    "not authorized",
    "no active membership",
    "subscription is not active",
    "not allowed",
    "out of session",
    "class is full",
    "fully booked",
    "no spots",
    "no spot available",
    "no available spots",
    "sold out",
    "no longer available",
    "capacity",
    "no space",
    "waitlist",
)


def _is_user_terminal(msg: str) -> bool:
    low = (msg or "").lower()
    return any(kw in low for kw in USER_TERMINAL_KEYWORDS)


# Coarse buckets for the "why did this rule fail?" insight. Pure/no-I/O so it
# runs over historical BookingAttempt.message rows too. Order matters: the most
# specific phrases win — "out of sessions" before generic terms, and class_full
# before window_not_open so "class full at window open" classifies as full.
_REASON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("out_of_credits", ("out of session", "no sessions", "out of credits", "exceeded")),
    ("already_booked", ("already reserved",)),
    ("slot_gone", ("disappeared", "no longer on schedule")),
    (
        "class_full",
        (
            "class full", "class is full", "fully booked", "sold out",
            "no spots", "no spot", "no available spots", "capacity",
            "waitlist", "no space", "no longer available", "registration cap",
        ),
    ),
    ("window_not_open", ("registration has not", "not yet started", "window")),
)


def _classify_reason(message: str) -> str:
    """Map a BookingAttempt message/reason to a coarse bucket for insight."""
    low = (message or "").lower()
    for bucket, keywords in _REASON_RULES:
        if any(kw in low for kw in keywords):
            return bucket
    return "other"


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


def schedule_credit_warning() -> None:
    """Daily 08:55 cron — five minutes before the digest flush — that queues an
    overbook warning into the digest buffer when scheduled bookings before
    period_end exceed remaining credits. Riding the 09:00 digest keeps it to at
    most one Telegram per day."""
    sch = get_scheduler()
    sch.add_job(
        credit_warning_job,
        "cron",
        hour=8,
        minute=55,
        id="credit-warning",
        replace_existing=True,
    )
    logger.info("Credit warning cron scheduled at 08:55 {}", settings.tz)


async def credit_warning_job() -> None:
    """Queue a heads-up (for the 09:00 digest) when the automations will attempt
    more bookings before period_end than there are credits left, and period_end
    is within WARNING_LEAD_DAYS. Fails silent on any lookup error — a broken
    forecast must never crash the cron."""
    try:
        usage = await pushpress.list_subscription_usage()
    except Exception as e:
        logger.warning("credit warning: usage lookup failed: {}", e)
        return
    gating = _gating_usage(usage)
    if gating is None or gating.remaining is None:
        return
    period_end = _parse_period_end(gating.period_end)
    if period_end is None:
        return
    days_left = (period_end - datetime.now(tz()).date()).days
    if days_left < 0 or days_left > WARNING_LEAD_DAYS:
        return
    total, per_rule = await scheduled_bookings_before_period_end(usage)
    if total <= gating.remaining:
        return
    names = ", ".join(f"{r['name']} (×{r['count']})" for r in per_rule)
    notify.queue_for_digest(
        f"⚠️ Credit overbook: {total} bookings scheduled before {gating.period_end} "
        f"but only {gating.remaining} credit(s) left. Rules: {names}. "
        f"Consider pausing one via pause_automation_rule."
    )


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


def _rule_matches(rule: AutomationRule, s, now: datetime) -> bool:
    """Slot/rule predicate shared by find_next_matching_slot and horizon_scan."""
    if rule.location and s.location.lower() != rule.location.lower():
        return False
    if rule.class_category and s.category.lower() != rule.class_category.lower():
        return False
    local = s.start.astimezone(tz())
    if local.weekday() != rule.day_of_week:
        return False
    if local.time().hour != rule.time_of_day.hour:
        return False
    if local.time().minute != rule.time_of_day.minute:
        return False
    if local <= now:
        return False
    return not (rule.paused_until and local.date() <= rule.paused_until)


async def find_next_matching_slot(
    rule: AutomationRule, after: datetime | None = None
):
    """Look up the next class in PushPress matching this rule. Returns
    (ClassSlot, target_datetime) or (None, target_datetime_hint).

    Kept for backward compat with manual-fire and the backup-chain lookup,
    which both need a single "next" candidate. Horizon scheduling uses
    find_all_matching_slots instead."""
    after = after or datetime.now(tz())
    hint = next_class_datetime(rule, now=after)
    start = hint.date() - timedelta(days=1)
    end = hint.date() + timedelta(days=LOOKAHEAD_DAYS)
    try:
        slots = await pushpress.list_schedule(start, end)
    except Exception as e:
        logger.warning("schedule fetch for rule {} failed: {}", rule.id, e)
        return None, hint
    candidates = [s for s in slots if _rule_matches(rule, s, after)]
    candidates.sort(key=lambda s: s.start)
    return (candidates[0] if candidates else None), hint


def find_all_matching_slots(rule: AutomationRule, all_slots, now: datetime) -> list:
    """Filter a pre-fetched schedule down to every slot this rule wants to
    book within the lookahead. Sorted by start time."""
    matches = [s for s in all_slots if _rule_matches(rule, s, now)]
    matches.sort(key=lambda s: s.start)
    return matches


def window_open_time(slot) -> datetime:
    """When does the booking window open for this slot? Uses the slot's own
    registrationStartOffset (negative minutes; e.g. -20160 for 14 days)."""
    offset_min = slot.registration_start_offset_min or 0
    return slot.start.astimezone(tz()) + timedelta(minutes=offset_min)


# --- Credit awareness ------------------------------------------------------ #
def _gating_usage(usage: list) -> pushpress.SubscriptionUsage | None:
    """The subscription that actually constrains booking: the finite-cap sub
    with the fewest remaining sessions. None when no sub exposes a finite cap
    (nothing to gate on)."""
    finite = [u for u in usage if u.remaining is not None]
    if not finite:
        return None
    return min(finite, key=lambda u: u.remaining)


def _parse_period_end(period_end: str | None) -> date | None:
    """PushPress periodEnd is an ISO date string ('2026-07-16'); tolerate a
    datetime prefix or an empty/None value. None when unparseable."""
    if not period_end:
        return None
    try:
        return date.fromisoformat(period_end[:10])
    except ValueError:
        return None


async def _credit_gate() -> tuple[bool, str | None]:
    """Decide whether a booking should be skipped because the session-credit
    budget is exhausted. Returns (blocked, period_end).

    blocked is True only when the gating subscription reports remaining <= 0.
    Fails OPEN: if the usage lookup raises (creds unset, API down) we return
    (False, None) so a broken credit check never blocks a legit booking."""
    try:
        usage = await pushpress.list_subscription_usage()
    except Exception as e:
        logger.warning("credit pre-check failed, proceeding with booking: {}", e)
        return False, None
    gating = _gating_usage(usage)
    if gating is None or gating.remaining is None:
        return False, None
    if gating.remaining <= 0:
        return True, (gating.period_end or None)
    return False, None


async def scheduled_bookings_before_period_end(
    usage: list, now: datetime | None = None
) -> tuple[int, list[dict]]:
    """How many bookings the active primary rules will attempt on or before the
    gating subscription's period_end, excluding already-reserved slots.

    One schedule fetch + one reservations fetch, reused across every rule, so
    cost doesn't scale with rule count. Returns (total, per_rule) where per_rule
    is a list of {rule_id, name, count, slots:[iso...]}. Returns (0, []) when
    there's no finite-cap sub or period_end is unparseable. find_all_matching_slots
    already applies the paused_until + future filters; we only add the
    period_end + already-booked cuts here."""
    now = now or datetime.now(tz())
    gating = _gating_usage(usage)
    if gating is None:
        return 0, []
    period_end = _parse_period_end(gating.period_end)
    if period_end is None:
        return 0, []

    try:
        all_slots = await pushpress.list_schedule(now.date(), period_end + timedelta(days=1))
    except Exception as e:
        logger.warning("forecast: schedule fetch failed: {}", e)
        return 0, []
    try:
        reservations = await pushpress.list_reservations()
        booked_ids = {r.class_id for r in reservations}
    except Exception as e:
        logger.warning("forecast: reservations fetch failed: {}", e)
        booked_ids = set()

    with DbSession(engine) as db:
        rules = db.exec(
            select(AutomationRule).where(
                AutomationRule.enabled == True,  # noqa: E712
                AutomationRule.backup_only == False,  # noqa: E712
            )
        ).all()

    total = 0
    per_rule: list[dict] = []
    for rule in rules:
        wanted = [
            s for s in find_all_matching_slots(rule, all_slots, now)
            if s.start.astimezone(tz()).date() <= period_end and s.id not in booked_ids
        ]
        if not wanted:
            continue
        total += len(wanted)
        per_rule.append({
            "rule_id": rule.id,
            "name": rule.name,
            "count": len(wanted),
            "slots": [s.start.astimezone(tz()).isoformat() for s in wanted],
        })
    return total, per_rule


async def horizon_refresh_all() -> None:
    """Refresh every enabled primary rule's horizon. Performs ONE schedule
    fetch + ONE reservations fetch and reuses them across rules, so adding
    a rule (or having ten) doesn't multiply API calls."""
    sch = get_scheduler()
    # Clean slot-jobs upfront; horizon_scan adds the ones we still want.
    for job in list(sch.get_jobs()):
        if job.id.startswith("rule-") and "-slot-" in job.id:
            job.remove()
        elif job.id.startswith("rule-") and job.id.endswith("-poll"):
            # Legacy poll jobs from before the horizon migration.
            job.remove()
        elif job.id.startswith("rule-") and "-retry-" not in job.id and "-slot-" not in job.id:
            # Legacy single-fire `rule-{id}` jobs from the pre-horizon era.
            job.remove()

    with DbSession(engine) as db:
        rules = db.exec(
            select(AutomationRule).where(
                AutomationRule.enabled == True,  # noqa: E712
                AutomationRule.backup_only == False,  # noqa: E712
            )
        ).all()

    if not rules:
        return

    # One schedule fetch shared across all rules.
    today = datetime.now(tz()).date()
    try:
        all_slots = await pushpress.list_schedule(today, today + timedelta(days=LOOKAHEAD_DAYS))
    except Exception as e:
        logger.warning("horizon_refresh_all: schedule fetch failed: {}", e)
        return

    # One reservations fetch — used to skip already-booked slots so we don't
    # spam PushPress with idempotent re-attempts.
    try:
        reservations = await pushpress.list_reservations()
        booked_class_ids = {r.class_id for r in reservations}
    except Exception as e:
        logger.warning("horizon_refresh_all: reservations fetch failed: {}", e)
        booked_class_ids = set()

    for rule in rules:
        await horizon_scan(rule, all_slots=all_slots, booked_class_ids=booked_class_ids)


async def horizon_scan(
    rule: AutomationRule,
    all_slots=None,
    booked_class_ids: set[str] | None = None,
) -> None:
    """Queue a booking_window_job for every matching upcoming slot the rule
    doesn't already have a reservation for. Idempotent: re-running replaces
    existing slot-jobs (same job_id) and adds any new ones; orphan jobs for
    slots no longer in the schedule were already cleaned by the caller's
    horizon_refresh_all sweep.

    `all_slots` and `booked_class_ids` are optional — they let the caller
    share one PushPress fetch across many rules. When None we fetch our own."""
    sch = get_scheduler()
    now = datetime.now(tz())

    if all_slots is None:
        today = now.date()
        try:
            all_slots = await pushpress.list_schedule(
                today, today + timedelta(days=LOOKAHEAD_DAYS)
            )
        except Exception as e:
            logger.warning("horizon_scan rule {}: schedule fetch failed: {}", rule.id, e)
            return

    if booked_class_ids is None:
        try:
            reservations = await pushpress.list_reservations()
            booked_class_ids = {r.class_id for r in reservations}
        except Exception as e:
            logger.warning("horizon_scan rule {}: reservations fetch failed: {}", rule.id, e)
            booked_class_ids = set()

    matches = find_all_matching_slots(rule, all_slots, now)
    if not matches:
        logger.info(
            "Rule {} ('{}'): no matching classes in next {} days",
            rule.id, rule.name, LOOKAHEAD_DAYS,
        )
        return

    queued = 0
    skipped_booked = 0
    for slot in matches:
        if slot.id in booked_class_ids:
            skipped_booked += 1
            continue
        fire_at = window_open_time(slot)
        if fire_at <= now:
            fire_at = now + timedelta(seconds=2)
        sch.add_job(
            booking_window_job,
            "date",
            run_date=fire_at,
            args=[rule.id, 0, slot.id],
            id=f"rule-{rule.id}-slot-{slot.id}",
            replace_existing=True,
            misfire_grace_time=60,
        )
        queued += 1
    logger.info(
        "Rule {} ('{}'): queued {} bookings, skipped {} already-booked (of {} matching)",
        rule.id, rule.name, queued, skipped_booked, len(matches),
    )


def schedule_horizon_refresh() -> None:
    """Daily 03:30 cron that re-runs horizon_refresh_all to pick up newly-opened
    windows and schedule changes. 03:30 sits between token refresh (03:00) and
    daily digest (09:00) so the digest captures whatever changed overnight."""
    sch = get_scheduler()
    sch.add_job(
        horizon_refresh_all,
        "cron",
        hour=3,
        minute=30,
        id="horizon-refresh",
        replace_existing=True,
    )
    logger.info("Horizon refresh cron scheduled daily at 03:30 {}", settings.tz)


# Backward-compat aliases for callers that still reference the old names.
reschedule_all = horizon_refresh_all


async def schedule_rule(rule: AutomationRule) -> None:
    """Compat shim — queues a single rule's full horizon. Used by code paths
    that mutate one rule and want to refresh just that rule's jobs."""
    await horizon_scan(rule)


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
) -> str:
    """One-shot job: try to book the specific class this rule was queued for.
    Returns a short outcome string ("booked", "skipped: out of credits…",
    "failed: …", …) so callers like fire_automation_rule can report it; the
    cron scheduler simply ignores the return value.
    Branches:
      - success → log + notify (no re-arm; the daily horizon cron handles that)
      - out of credits → skip + record "skipped" + notify (no chain; a backup
        hits the same flat period cap)
      - class full at window-open → chain to backup if set, else flag user
      - "already reserved" → silent no-op (idempotent, we have the booking)
      - user-terminal error → maybe chain + give up
      - transient → retry every 30s up to MAX_RETRIES, then maybe chain

    `calendar_item_uuid` is the slot horizon_scan queued this job for — the
    source of truth. We do NOT re-resolve "next matching slot" here, which
    historically caused infinite loops on the idempotent reservation.

    `chained_from` carries rule IDs already tried in the backup chain for
    cycle detection. `manual` bypasses the loop guard so a user-initiated
    fire isn't suppressed by a recent scheduled fire of the same rule."""
    with DbSession(engine) as db:
        rule = db.get(AutomationRule, rule_id)
        if not rule or not rule.enabled:
            logger.info("Rule {} missing or disabled, skipping", rule_id)
            return "rule missing or disabled"

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
            return "loop-guarded"
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
        return "target class disappeared before booking"

    class_start = slot.start.astimezone(tz())
    target_label = f"{rule.name} — {class_start.strftime('%a %d %b %H:%M')}"
    logger.info("Firing rule {} attempt {}: {}", rule_id, attempt + 1, target_label)

    # Class full at window-open: chain to backup if set, else flag the user.
    # Polling was removed — the horizon scan + backup chain replace it.
    if (slot.spots_available or 0) <= 0:
        reason = (
            f"class full at window open "
            f"({slot.spots_available}/{slot.spots_total})"
        )
        await _give_up_this_week(
            rule, slot, target_label, reason,
            chained_from=chained_from,
            notify_immediate=rule.backup_rule_id is None,
        )
        return reason

    # Credit pre-check: the single choke point every booking path converges on
    # (scheduled, manual fire, backup chain). If the session-credit budget is
    # spent, skip rather than burn a guaranteed-fail API call. No backup chain —
    # a backup hits the same flat period cap. Fails open (see _credit_gate).
    blocked, period_end = await _credit_gate()
    if blocked:
        tail = f", period ends {period_end}" if period_end else ""
        reason = f"skipped: out of credits{tail}"
        logger.info("Rule {}: {} — not booking {}", rule_id, reason, target_label)
        _record(rule.id, target_label, "skipped", reason)
        await notify.send(f"⏸️ {rule.name}: {reason}")
        return reason

    try:
        result = await pushpress.book(slot.id)
    except Exception as e:
        msg = f"book call raised: {e}"
        await _handle_failure(
            rule, attempt, target_label, msg,
            slot=slot, chained_from=chained_from,
        )
        return f"error: {msg}"
    if result.ok:
        _record(rule.id, target_label, "success", result.message or "booked")
        await notify.send(f"✅ Booked: {target_label}")
        # Bump the reminder scan so reminders for THIS new reservation land
        # immediately instead of waiting up to 15 min for the next interval.
        await reminder_scan_job()
        # No per-success re-arm: every matching slot in the horizon already
        # has its own queued job. The daily horizon cron catches anything
        # the in-process scheduler missed (e.g. across a restart).
        return "booked"

    await _handle_failure(
        rule, attempt, target_label, result.message,
        slot=slot, chained_from=chained_from,
    )
    return f"failed: {result.message}"


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


async def _handle_failure(
    rule: AutomationRule,
    attempt: int,
    label: str,
    msg: str,
    slot=None,
    chained_from: set[int] | None = None,
) -> None:
    logger.warning("Rule {} attempt {} failed: {}", rule.id, attempt + 1, msg)

    low = (msg or "").lower()

    # "Already reserved" is idempotent — we have the booking, nothing to do.
    # Critically: do NOT chain to backup here, because that would book a backup
    # class even though the primary is already secured.
    if "already reserved" in low:
        logger.info("Rule {} attempt {}: {} — already booked, silent skip",
                    rule.id, attempt + 1, label)
        _record(rule.id, label, "success", "already reserved (idempotent)")
        return

    # User-side terminal → give up on THIS slot (and chain to backup if set).
    # Class-full is treated the same as any other terminal: chain or flag.
    # Polling was removed — backups replace the cancellation-watch path.
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
    """Single exit point for 'this slot can't be booked'. Records the failure
    and fires the backup chain if one is configured. The horizon model already
    has the rule's other matching slots queued, so there's no re-arm to do
    here — each week is an independent job.

    `chained_from` carries the visited set so a chained give-up doesn't loop.
    `notify_immediate=False` routes the user-facing message to the daily digest
    for quiet failures where the user has nothing to act on right now."""
    _record(rule.id, target_label, "failure", reason)
    visited = (chained_from or set()) | {rule.id}
    if slot is not None:
        target_start = slot.start.astimezone(tz())
    else:
        target_start = next_class_datetime(rule, datetime.now(tz()))

    chained = await _chain_to_backup(rule, target_start, visited, reason)
    if not chained:
        # No backup picked it up — flag the user.
        msg = f"❌ {rule.name}: {reason}"
        if notify_immediate:
            await notify.send(msg)
        else:
            notify.queue_for_digest(msg)


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
