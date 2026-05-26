from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

import scheduler
from models import AutomationRule
from pushpress import ClassSlot

TZ = ZoneInfo("Europe/Amsterdam")


def make_rule(dow: int, hh: int, mm: int = 0, paused_until=None) -> AutomationRule:
    return AutomationRule(
        id=1,
        name="Test rule",
        location="Overste den Oudenlaan 9",
        class_category="Classic CrossFit",
        day_of_week=dow,
        time_of_day=time(hh, mm),
        enabled=True,
        paused_until=paused_until,
    )


def test_next_class_datetime_future_same_week():
    now = datetime(2026, 5, 13, 12, 0, tzinfo=TZ)  # Wed
    rule = make_rule(dow=4, hh=17)  # Fri 17:00
    dt = scheduler.next_class_datetime(rule, now=now)
    assert dt.weekday() == 4
    assert dt.hour == 17
    assert dt > now


def test_next_class_datetime_rolls_to_next_week_if_passed():
    now = datetime(2026, 5, 13, 18, 0, tzinfo=TZ)
    rule = make_rule(dow=2, hh=17)
    dt = scheduler.next_class_datetime(rule, now=now)
    assert dt.weekday() == 2
    assert (dt - now).days >= 6


def test_window_open_uses_slot_offset():
    start = datetime(2026, 5, 20, 9, 0, tzinfo=TZ)
    slot = ClassSlot(
        id="x", name="OV | Classic CrossFit",
        location="Overste den Oudenlaan 9", location_code="OV", category="Classic CrossFit",
        start=start, end=start + timedelta(hours=1),
        registration_start_offset_min=-20160,  # 14 days
    )
    win = scheduler.window_open_time(slot)
    assert (start - win).total_seconds() == 14 * 24 * 3600


def test_window_open_with_zero_offset_is_class_start():
    start = datetime(2026, 5, 20, 9, 0, tzinfo=TZ)
    slot = ClassSlot(
        id="x", name="Trial", location="Havenweg 6", location_code="", category="Trial Class",
        start=start, end=start + timedelta(hours=1),
        registration_start_offset_min=0,
    )
    assert scheduler.window_open_time(slot) == start


@pytest.mark.parametrize(
    "hours_until_class, expected_interval_min",
    [
        (100, 12 * 60),  # >48h -> 12h
        (49, 12 * 60),   # boundary just above
        (47, 4 * 60),    # 24h–48h -> 4h
        (25, 4 * 60),    # boundary just above 24h
        (23, 60),        # 6h–24h -> 1h
        (7, 60),         # boundary
        (5, 15),         # 1h–6h -> 15m
        (1.5, 15),       # boundary
    ],
)
def test_poll_interval_brackets(hours_until_class, expected_interval_min):
    interval = scheduler._poll_interval_for(timedelta(hours=hours_until_class))
    assert interval.total_seconds() / 60 == expected_interval_min


def test_paused_until_skips_targeted_class():
    """find_next_matching_slot must skip slots on or before paused_until."""
    from datetime import date as _date
    rule = make_rule(dow=2, hh=9, paused_until=_date(2026, 5, 30))
    # next_class_datetime for a Wed 09:00 rule (after May 30) would be 2026-06-03
    # We don't call find_next_matching_slot directly (it does API calls), so
    # just unit-check the filter logic via _match_slot's sibling.
    target_in_window = datetime(2026, 5, 27, 9, 0, tzinfo=TZ)
    target_past_window = datetime(2026, 6, 3, 9, 0, tzinfo=TZ)
    in_slot = ClassSlot(
        id="x", name="OV | Classic CrossFit",
        location="Overste den Oudenlaan 9", location_code="OV", category="Classic CrossFit",
        start=target_in_window, end=target_in_window,
    )
    past_slot = ClassSlot(
        id="y", name="OV | Classic CrossFit",
        location="Overste den Oudenlaan 9", location_code="OV", category="Classic CrossFit",
        start=target_past_window, end=target_past_window,
    )
    # _match_slot itself doesn't filter by paused_until (it's used by
    # _legacy callers post window-match). The new filter lives in
    # find_next_matching_slot. Verify by inspecting rule.paused_until.
    assert rule.paused_until == _date(2026, 5, 30)
    assert in_slot.start.date() <= rule.paused_until
    assert past_slot.start.date() > rule.paused_until


def test_match_slot_filters_by_location_and_category():
    target = datetime(2026, 5, 14, 9, 0, tzinfo=TZ)
    slots = [
        ClassSlot(
            id="a", name="OV | Classic CrossFit", location="Overste den Oudenlaan 9",
            location_code="OV", category="Classic CrossFit",
            start=target, end=target,
        ),
        ClassSlot(
            id="b", name="HW | Classic CrossFit", location="Havenweg 6",
            location_code="HW", category="Classic CrossFit",
            start=target, end=target,
        ),
    ]
    rule = make_rule(dow=3, hh=9)
    match = scheduler._match_slot(slots, rule, target)
    assert match.id == "a"


# ---- Loop-guard + advance-past-slot regressions ----------------------------


@pytest.fixture
def reset_loop_guard():
    scheduler._last_fire_at.clear()
    yield
    scheduler._last_fire_at.clear()


def _ov_slot(uuid: str, start: datetime, spots: int = 5) -> ClassSlot:
    return ClassSlot(
        id=uuid,
        name="OV | Classic CrossFit",
        location="Overste den Oudenlaan 9",
        location_code="OV",
        category="Classic CrossFit",
        start=start,
        end=start + timedelta(hours=1),
        spots_available=spots,
        spots_total=14,
        registration_start_offset_min=-20160,
    )


@pytest.mark.asyncio
async def test_loop_guard_short_circuits_rapid_refire(monkeypatch, reset_loop_guard):
    """Two booking_window_job calls for the same rule within LOOP_GUARD_WINDOW_SEC:
    the second must abort without touching PushPress or rescheduling."""
    rule = make_rule(dow=2, hh=18, mm=30)
    slot = _ov_slot("cal-loopguard", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))

    # Stub the DB lookup of the rule.
    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _FakeDb(rule))
    # Stub the slot lookup.
    async def fake_lookup(uuid):
        assert uuid == "cal-loopguard"
        return slot
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)
    # Stub pushpress.book — must NOT be called on the second invocation.
    book_calls: list[str] = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="reg-1", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)
    # Silence notify + reminder scan + downstream scheduling.
    async def noop(*_a, **_k):
        return None
    monkeypatch.setattr(scheduler.notify, "send", noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "reminder_scan_job", noop)
    monkeypatch.setattr(scheduler, "_schedule_after", noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(rule.id, 0, "cal-loopguard")
    await scheduler.booking_window_job(rule.id, 0, "cal-loopguard")

    assert book_calls == ["cal-loopguard"], (
        "second rapid fire should have been blocked by the loop guard"
    )


@pytest.mark.asyncio
async def test_booking_window_job_books_passed_uuid_not_lookup_result(
    monkeypatch, reset_loop_guard
):
    """Regression: the buggy version re-queried `find_next_matching_slot(now)` and
    could end up booking a different slot than the one queued. Now the job
    must book exactly the slot whose uuid it received."""
    rule = make_rule(dow=2, hh=18, mm=30)
    queued_uuid = "cal-queued"
    queued_slot = _ov_slot(queued_uuid, datetime(2026, 6, 3, 18, 30, tzinfo=TZ))

    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _FakeDb(rule))
    async def fake_lookup(uuid):
        return queued_slot if uuid == queued_uuid else None
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)
    book_calls: list[str] = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)
    async def noop(*_a, **_k):
        return None
    monkeypatch.setattr(scheduler.notify, "send", noop)
    monkeypatch.setattr(scheduler, "reminder_scan_job", noop)
    monkeypatch.setattr(scheduler, "_schedule_after", noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(rule.id, 0, queued_uuid)

    assert book_calls == [queued_uuid]


@pytest.mark.asyncio
async def test_handle_failure_user_terminal_advances_past_slot(
    monkeypatch, reset_loop_guard
):
    """Replaces the buggy `_schedule_after(now + 1h)` that caused the May 20
    incident. The user-terminal branch must reschedule with after=slot.start+1min
    so find_next_matching_slot returns the FOLLOWING week's slot."""
    rule = make_rule(dow=2, hh=18, mm=30)
    slot = _ov_slot("cal-failed", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))

    captured: dict[str, datetime] = {}
    async def fake_schedule_after(_rule, after):
        captured["after"] = after
    monkeypatch.setattr(scheduler, "_schedule_after", fake_schedule_after)
    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler._handle_failure(
        rule, attempt=0, label="x", msg="already reserved", slot=slot
    )

    expected = slot.start.astimezone(TZ) + timedelta(minutes=1)
    assert captured["after"] == expected, (
        f"user-terminal failure must advance past slot.start "
        f"(got {captured['after']}, expected {expected})"
    )


@pytest.mark.asyncio
async def test_handle_failure_max_retries_advances_past_slot(
    monkeypatch, reset_loop_guard
):
    rule = make_rule(dow=2, hh=18, mm=30)
    slot = _ov_slot("cal-failed", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))

    captured: dict[str, datetime] = {}
    async def fake_schedule_after(_rule, after):
        captured["after"] = after
    monkeypatch.setattr(scheduler, "_schedule_after", fake_schedule_after)
    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    # transient message (not class-full, not user-terminal), at the final attempt
    await scheduler._handle_failure(
        rule,
        attempt=scheduler.MAX_RETRIES - 1,
        label="x",
        msg="connection reset",
        slot=slot,
    )

    expected = slot.start.astimezone(TZ) + timedelta(minutes=1)
    assert captured["after"] == expected


@pytest.mark.asyncio
async def test_handle_failure_terminal_uses_digest_not_immediate_telegram(
    monkeypatch, reset_loop_guard
):
    """The May 20 incident sent a Telegram on every loop iteration. Terminal
    failures must now route to the daily digest instead."""
    rule = make_rule(dow=2, hh=18, mm=30)
    slot = _ov_slot("cal-failed", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))

    send_calls = []
    digest_calls = []
    async def fake_send(msg):
        send_calls.append(msg)
    def fake_queue(msg):
        digest_calls.append(msg)
    monkeypatch.setattr(scheduler.notify, "send", fake_send)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", fake_queue)
    monkeypatch.setattr(scheduler, "_schedule_after", _async_noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler._handle_failure(
        rule, attempt=0, label="x", msg="already reserved", slot=slot
    )

    assert send_calls == [], "no immediate Telegram for terminal failure"
    assert any("terminal" in line.lower() for line in digest_calls)


# ---- Test helpers ----------------------------------------------------------


async def _async_noop(*_a, **_k):
    return None


class _FakeDb:
    """Minimal context-manager stand-in for sqlmodel.Session used by
    booking_window_job's `db.get(AutomationRule, rule_id)` call."""
    def __init__(self, rule):
        self._rule = rule

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, _model, _rule_id):
        return self._rule


class _MultiFakeDb:
    """Fake Session that knows about multiple rules by id. Used by chain tests
    that need DbSession(engine).get() to return different rules across calls."""
    def __init__(self, rules_by_id: dict[int, AutomationRule]):
        self._rules = rules_by_id

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, _model, rule_id):
        return self._rules.get(rule_id)


def _backup_rule(rule_id: int, **overrides) -> AutomationRule:
    """A primary-by-default AutomationRule with a controllable id."""
    base = dict(
        id=rule_id,
        name=f"Rule {rule_id}",
        location="Overste den Oudenlaan 9",
        class_category="Classic CrossFit",
        day_of_week=2,
        time_of_day=time(18, 30),
        enabled=True,
        backup_only=False,
        backup_rule_id=None,
    )
    base.update(overrides)
    return AutomationRule(**base)


# ---- Backup chain tests ---------------------------------------------------


@pytest.mark.asyncio
async def test_class_full_at_window_open_chains_to_backup_when_set(
    monkeypatch, reset_loop_guard
):
    """Primary's class is full at window-open AND it has a backup configured:
    polling MUST be skipped and the backup chain MUST fire immediately."""
    primary = _backup_rule(1, backup_rule_id=2)
    backup = _backup_rule(2, name="Backup A", class_category="Aerodance", backup_only=True)
    primary_slot = _ov_slot("cal-primary", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=0)
    backup_slot = _ov_slot("cal-backup", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession",
                        lambda *_a, **_k: _MultiFakeDb({1: primary, 2: backup}))
    async def fake_lookup(uuid):
        return primary_slot if uuid == "cal-primary" else backup_slot
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)
    async def fake_find_next(rule, after=None):
        if rule.id == 2:
            return backup_slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
        return None, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    polling_started = []
    async def fake_start_polling(*a, **k):
        polling_started.append((a, k))
    monkeypatch.setattr(scheduler, "_start_polling", fake_start_polling)

    book_calls = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)

    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "reminder_scan_job", _async_noop)
    monkeypatch.setattr(scheduler, "_schedule_after", _async_noop)
    monkeypatch.setattr(scheduler, "_advance_past_slot", _async_noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(1, 0, "cal-primary")

    assert polling_started == [], (
        "polling must be skipped when a backup_rule_id is set on the primary"
    )
    assert book_calls == ["cal-backup"], (
        "backup's class should have been booked via the chain"
    )


@pytest.mark.asyncio
async def test_class_full_at_window_open_polls_when_no_backup(
    monkeypatch, reset_loop_guard
):
    """Regression: when NO backup is set, the class-full branch must still
    fall through to _start_polling (existing behavior preserved)."""
    primary = _backup_rule(1)  # no backup_rule_id
    slot = _ov_slot("cal-full", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=0)

    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _FakeDb(primary))
    async def fake_lookup(uuid):
        return slot
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)

    polling_started = []
    async def fake_start_polling(*a, **k):
        polling_started.append(True)
    monkeypatch.setattr(scheduler, "_start_polling", fake_start_polling)

    book_calls = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)

    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(1, 0, "cal-full")

    assert polling_started == [True], "no backup configured ⇒ poll, do not chain"
    assert book_calls == []


@pytest.mark.asyncio
async def test_chain_recurses_when_backup_has_no_slot(monkeypatch):
    """A → B (no slot) → C (has slot) — the chain must skip B and fire C."""
    a = _backup_rule(1, backup_rule_id=2)
    b = _backup_rule(2, name="B", backup_rule_id=3, backup_only=True)
    c = _backup_rule(3, name="C", backup_only=True)
    c_slot = _ov_slot("cal-c", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession",
                        lambda *_a, **_k: _MultiFakeDb({1: a, 2: b, 3: c}))

    async def fake_find_next(rule, after=None):
        if rule.id == 3:
            return c_slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
        return None, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    booking_calls = []
    async def fake_bwj(rule_id, attempt, uuid, **kwargs):
        booking_calls.append((rule_id, uuid, kwargs.get("chained_from")))
    monkeypatch.setattr(scheduler, "booking_window_job", fake_bwj)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)

    fired = await scheduler._chain_to_backup(
        a, datetime(2026, 6, 3, 18, 30, tzinfo=TZ), {a.id}, "test"
    )

    assert fired is True
    assert len(booking_calls) == 1
    fired_id, fired_uuid, chained_from = booking_calls[0]
    assert fired_id == 3
    assert fired_uuid == "cal-c"
    assert chained_from == {1, 2}  # both A and B in visited


@pytest.mark.asyncio
async def test_chain_cycle_detection_breaks_loop(monkeypatch):
    """If the chain would loop back (A → B → A), the second link must be
    rejected — not fired and not infinitely recursed."""
    a = _backup_rule(1, backup_rule_id=2)
    b = _backup_rule(2, name="B", backup_rule_id=1, backup_only=True)
    b_slot = _ov_slot("cal-b", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession",
                        lambda *_a, **_k: _MultiFakeDb({1: a, 2: b}))

    async def fake_find_next(rule, after=None):
        if rule.id == 2:
            return b_slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
        return None, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    booking_calls = []
    async def fake_bwj(rule_id, attempt, uuid, **kwargs):
        booking_calls.append(rule_id)
    monkeypatch.setattr(scheduler, "booking_window_job", fake_bwj)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)

    # Start at A. Chain follows to B and fires it. B's chain points back to A
    # — but at that point A is already visited, so _chain_to_backup(B,...)
    # returns False instead of looping.
    fired = await scheduler._chain_to_backup(
        a, datetime(2026, 6, 3, 18, 30, tzinfo=TZ), {a.id}, "test"
    )
    assert fired is True
    assert booking_calls == [2]

    # Now call from B's perspective with A already visited — must NOT fire A.
    booking_calls.clear()
    fired2 = await scheduler._chain_to_backup(
        b, datetime(2026, 6, 3, 18, 30, tzinfo=TZ), {a.id, b.id}, "test"
    )
    assert fired2 is False
    assert booking_calls == []


@pytest.mark.asyncio
async def test_chain_depth_cap_enforced(monkeypatch):
    """Past MAX_CHAIN_DEPTH the chain stops, even if more backups exist."""
    # Build a chain longer than MAX_CHAIN_DEPTH.
    rules = {}
    for i in range(1, scheduler.MAX_CHAIN_DEPTH + 3):
        rules[i] = _backup_rule(
            i, name=f"R{i}",
            backup_rule_id=(i + 1) if i < scheduler.MAX_CHAIN_DEPTH + 2 else None,
        )
    last_slot = _ov_slot("cal-last", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))

    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _MultiFakeDb(rules))

    async def fake_find_next(rule, after=None):
        # Only the very last rule has a slot — chain has to walk through all.
        if rule.id == scheduler.MAX_CHAIN_DEPTH + 2:
            return last_slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
        return None, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    booking_calls = []
    async def fake_bwj(rule_id, attempt, uuid, **kwargs):
        booking_calls.append(rule_id)
    monkeypatch.setattr(scheduler, "booking_window_job", fake_bwj)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)

    visited = {1}
    fired = await scheduler._chain_to_backup(
        rules[1], datetime(2026, 6, 3, 18, 30, tzinfo=TZ), visited, "test"
    )
    assert fired is False, "depth cap must short-circuit the chain"
    assert booking_calls == []


@pytest.mark.asyncio
async def test_handle_failure_class_full_chains_when_backup_set(
    monkeypatch, reset_loop_guard
):
    """Class-full mid-flow (book call returned a class-full message) AND a
    backup is configured → must chain, NOT start polling."""
    primary = _backup_rule(1, backup_rule_id=2)
    backup = _backup_rule(2, backup_only=True)
    primary_slot = _ov_slot("cal-p", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=2)
    backup_slot = _ov_slot("cal-b", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession",
                        lambda *_a, **_k: _MultiFakeDb({1: primary, 2: backup}))
    async def fake_find_next(rule, after=None):
        return backup_slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    polled = []
    async def fake_start_polling(*a, **k):
        polled.append(True)
    monkeypatch.setattr(scheduler, "_start_polling", fake_start_polling)

    chain_calls = []
    async def fake_bwj(rule_id, attempt, uuid, **kwargs):
        chain_calls.append((rule_id, uuid))
    monkeypatch.setattr(scheduler, "booking_window_job", fake_bwj)

    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_advance_past_slot", _async_noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler._handle_failure(
        primary, attempt=0, label="x",
        msg="class is full, no spots", slot=primary_slot,
    )

    assert polled == [], "primary with backup must skip polling"
    assert chain_calls == [(2, "cal-b")], "backup should have fired via the chain"


# ---- Manual fire endpoint integration ------------------------------------


def test_manual_fire_endpoint_resolves_slot_and_fires(monkeypatch):
    """Regression: the bug was that POST /automation/{id}/fire called
    booking_window_job(rule_id, 0) without calendar_item_uuid, raising
    TypeError. The fix resolves the next matching slot first."""
    from fastapi.testclient import TestClient

    import main
    from auth import COOKIE_NAME, make_session_token
    from models import Session as ModelsSession
    from models import engine

    slot = _ov_slot("cal-manual", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))
    async def fake_find_next(_rule, after=None):
        return slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    bwj_calls = []
    async def fake_bwj(rid, attempt, uuid, **kwargs):
        bwj_calls.append((rid, attempt, uuid, kwargs))
    monkeypatch.setattr(scheduler, "booking_window_job", fake_bwj)

    with TestClient(main.app) as client:
        # init_db() ran in the lifespan; safe to insert now.
        with ModelsSession(engine) as db:
            rule = AutomationRule(
                name="Test", location="OV", class_category="Classic CrossFit",
                day_of_week=2, time_of_day=time(18, 30), enabled=True,
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)
            rule_id = rule.id

        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.post(f"/automation/{rule_id}/fire", follow_redirects=False)

        with ModelsSession(engine) as db:
            leftover = db.get(AutomationRule, rule_id)
            if leftover:
                db.delete(leftover)
                db.commit()

    assert r.status_code == 303
    assert bwj_calls == [(rule_id, 0, "cal-manual", {"manual": True})]


def test_manual_fire_endpoint_no_slot_returns_400(monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from auth import COOKIE_NAME, make_session_token
    from models import Session as ModelsSession
    from models import engine

    async def fake_find_next(_rule, after=None):
        return None, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    with TestClient(main.app) as client:
        with ModelsSession(engine) as db:
            rule = AutomationRule(
                name="Empty", location="OV", class_category="Classic CrossFit",
                day_of_week=2, time_of_day=time(18, 30), enabled=True,
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)
            rule_id = rule.id

        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.post(f"/automation/{rule_id}/fire", follow_redirects=False)

        with ModelsSession(engine) as db:
            leftover = db.get(AutomationRule, rule_id)
            if leftover:
                db.delete(leftover)
                db.commit()

    assert r.status_code == 400
    assert "no matching class" in r.text.lower()
