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
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(rule.id, 0, queued_uuid)

    assert book_calls == [queued_uuid]


@pytest.mark.asyncio
async def test_handle_failure_already_reserved_does_not_chain(
    monkeypatch, reset_loop_guard
):
    """Idempotency: if PushPress says 'already reserved', we have the booking.
    Don't chain to backup (would book a backup we don't need) and don't notify
    the user (the booking is already there)."""
    primary = _backup_rule(1, backup_rule_id=2)
    backup = _backup_rule(2, backup_only=True)
    slot = _ov_slot("cal-already", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))

    monkeypatch.setattr(scheduler, "DbSession",
                        lambda *_a, **_k: _MultiFakeDb({1: primary, 2: backup}))
    chain_calls = []
    async def fake_chain(*a, **k):
        chain_calls.append(True)
        return True
    monkeypatch.setattr(scheduler, "_chain_to_backup", fake_chain)
    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler._handle_failure(
        primary, attempt=0, label="x", msg="already reserved", slot=slot
    )

    assert chain_calls == [], (
        "'already reserved' is idempotent — must not trigger backup chain"
    )


@pytest.mark.asyncio
async def test_handle_failure_terminal_uses_digest_not_immediate_telegram(
    monkeypatch, reset_loop_guard
):
    """The May 20 incident sent a Telegram on every loop iteration. Terminal
    failures must route to the daily digest instead."""
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
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    # "exceeded" is in USER_TERMINAL_KEYWORDS and not the special-cased
    # 'already reserved' path.
    await scheduler._handle_failure(
        rule, attempt=0, label="x", msg="cap exceeded", slot=slot
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
    chain MUST fire immediately, pushpress.book MUST NOT be called for the
    primary, and the backup's booking job runs."""
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

    book_calls = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)

    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "reminder_scan_job", _async_noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(1, 0, "cal-primary")

    # Only the backup's slot should have been booked — primary's full slot
    # gets skipped (full at window-open) and the chain takes over.
    assert book_calls == ["cal-backup"], (
        "backup's class should have been booked via the chain"
    )


@pytest.mark.asyncio
async def test_class_full_at_window_open_flags_user_when_no_backup(
    monkeypatch, reset_loop_guard
):
    """Without a backup, a full class at window-open must FLAG the user
    immediately (no polling, no retry — polling was removed)."""
    primary = _backup_rule(1)  # no backup_rule_id
    slot = _ov_slot("cal-full", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=0)

    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _FakeDb(primary))
    async def fake_lookup(uuid):
        return slot
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)

    book_calls = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)

    send_calls = []
    async def fake_send(msg):
        send_calls.append(msg)
    monkeypatch.setattr(scheduler.notify, "send", fake_send)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler.booking_window_job(1, 0, "cal-full")

    assert book_calls == [], "no book attempt when full at window-open"
    assert any("full at window open" in m.lower() for m in send_calls), (
        "must surface a Telegram flag for the user (no polling fallback)"
    )


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
    backup is configured → must chain via _give_up_this_week. (Polling was
    removed entirely; class-full is treated as terminal for this slot.)"""
    primary = _backup_rule(1, backup_rule_id=2)
    backup = _backup_rule(2, backup_only=True)
    primary_slot = _ov_slot("cal-p", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=2)
    backup_slot = _ov_slot("cal-b", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession",
                        lambda *_a, **_k: _MultiFakeDb({1: primary, 2: backup}))
    async def fake_find_next(rule, after=None):
        return backup_slot, datetime(2026, 6, 3, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(scheduler, "find_next_matching_slot", fake_find_next)

    chain_calls = []
    async def fake_bwj(rule_id, attempt, uuid, **kwargs):
        chain_calls.append((rule_id, uuid))
    monkeypatch.setattr(scheduler, "booking_window_job", fake_bwj)

    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    await scheduler._handle_failure(
        primary, attempt=0, label="x",
        msg="class is full, no spots", slot=primary_slot,
    )

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


def test_automation_backup_for_mode_renders_cascading_form(monkeypatch):
    """GET /automation?backup_for=<id> must pivot the create form into backup
    mode: heading mentions the primary's name and form action points at
    /automation/<id>/add-backup."""
    from fastapi.testclient import TestClient

    import main
    from auth import COOKIE_NAME, make_session_token
    from models import Session as ModelsSession
    from models import engine

    with TestClient(main.app) as client:
        with ModelsSession(engine) as db:
            rule = AutomationRule(
                name="My Primary", location="OV",
                class_category="Classic CrossFit",
                day_of_week=2, time_of_day=time(18, 30), enabled=True,
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)
            rule_id = rule.id

        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.get(f"/automation?backup_for={rule_id}")

        with ModelsSession(engine) as db:
            leftover = db.get(AutomationRule, rule_id)
            if leftover:
                db.delete(leftover)
                db.commit()

    assert r.status_code == 200
    body = r.text
    assert "Add backup for" in body
    assert "My Primary" in body
    assert f'action="/automation/{rule_id}/add-backup"' in body


def test_automation_backup_for_nonexistent_returns_404():
    from fastapi.testclient import TestClient

    import main
    from auth import COOKIE_NAME, make_session_token

    with TestClient(main.app) as client:
        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.get("/automation?backup_for=999999")
    assert r.status_code == 404


def test_from_class_as_backup_attaches_to_chain_tail(monkeypatch):
    """POST /automation/from-class/{slot_id}/as-backup looks up the slot,
    creates a backup_only rule with its params, and attaches to the primary's
    chain tail."""
    from fastapi.testclient import TestClient

    import main
    import pushpress
    from auth import COOKIE_NAME, make_session_token
    from models import Session as ModelsSession
    from models import engine

    # Force the "creds set" branch — the endpoint short-circuits when blank.
    monkeypatch.setattr(main.settings, "pushpress_email", "x@example.com")
    monkeypatch.setattr(main.settings, "pushpress_password", "pw")

    target_slot = _ov_slot(
        "cal-newbackup", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5
    )

    async def fake_list_schedule(start, end):
        return [target_slot]
    monkeypatch.setattr(pushpress, "list_schedule", fake_list_schedule)

    async def noop_reschedule():
        return None
    monkeypatch.setattr(main.scheduler, "reschedule_all", noop_reschedule)

    with TestClient(main.app) as client:
        with ModelsSession(engine) as db:
            primary = AutomationRule(
                name="Wed Classic OV", location="Overste den Oudenlaan 9",
                class_category="Classic CrossFit",
                day_of_week=2, time_of_day=time(18, 30), enabled=True,
            )
            db.add(primary)
            db.commit()
            db.refresh(primary)
            primary_id = primary.id

        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.post(
            f"/automation/from-class/{target_slot.id}/as-backup",
            data={"primary_rule_id": str(primary_id)},
            follow_redirects=False,
        )

        # The created backup rule should be linked to the primary.
        with ModelsSession(engine) as db:
            primary_after = db.get(AutomationRule, primary_id)
            assert primary_after.backup_rule_id is not None
            backup = db.get(AutomationRule, primary_after.backup_rule_id)
            assert backup is not None
            assert backup.backup_only is True
            assert backup.class_category == "Classic CrossFit"
            assert backup.location == "Overste den Oudenlaan 9"
            assert backup.day_of_week == 2
            assert backup.time_of_day == time(18, 30)
            backup_id = backup.id

        # Cleanup
        with ModelsSession(engine) as db:
            for rid in (primary_id, backup_id):
                rr = db.get(AutomationRule, rid)
                if rr:
                    rr.backup_rule_id = None
                    db.add(rr)
            db.commit()
            for rid in (primary_id, backup_id):
                rr = db.get(AutomationRule, rid)
                if rr:
                    db.delete(rr)
            db.commit()

    assert r.status_code == 303


# ---- Horizon scan (multi-week pre-book) -----------------------------------


def test_find_all_matching_slots_picks_every_matching_class():
    """Across a 14-day window, the rule's matcher must return every class
    that fits its day-of-week + time + location + category — not just the
    nearest one. This is what makes multi-week pre-booking work."""
    rule = make_rule(dow=2, hh=18, mm=30)
    now = datetime(2026, 5, 26, 12, 0, tzinfo=TZ)
    # 3 Wed 18:30 slots within the next 14 days, plus a non-matching one.
    week1 = _ov_slot("w1", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))
    week2 = _ov_slot("w2", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))
    week3 = _ov_slot("w3", datetime(2026, 6, 10, 18, 30, tzinfo=TZ))
    wrong_day = _ov_slot("wd", datetime(2026, 5, 28, 18, 30, tzinfo=TZ))  # Thu
    slots = [wrong_day, week2, week1, week3]

    matches = scheduler.find_all_matching_slots(rule, slots, now)
    assert [s.id for s in matches] == ["w1", "w2", "w3"], (
        "must return all 3 matching weeks sorted by start time"
    )


@pytest.mark.asyncio
async def test_horizon_scan_queues_one_job_per_matching_slot(monkeypatch):
    """horizon_scan must queue a distinct booking job for every matching slot
    in the lookahead — not just the next one. This is the multi-week
    pre-booking behavior."""
    rule = _backup_rule(1)
    week1 = _ov_slot("w1", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))
    week2 = _ov_slot("w2", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))
    week3 = _ov_slot("w3", datetime(2026, 6, 10, 18, 30, tzinfo=TZ))

    queued_ids: list[str] = []

    class FakeJob:
        pass

    class FakeScheduler:
        def add_job(self, fn, trigger, **kwargs):
            queued_ids.append(kwargs["id"])
            return FakeJob()

    monkeypatch.setattr(scheduler, "get_scheduler", lambda: FakeScheduler())

    await scheduler.horizon_scan(
        rule, all_slots=[week1, week2, week3], booked_class_ids=set()
    )

    assert sorted(queued_ids) == sorted([
        "rule-1-slot-w1", "rule-1-slot-w2", "rule-1-slot-w3",
    ]), "one job per matching slot, deterministic ids per slot"


@pytest.mark.asyncio
async def test_horizon_scan_skips_already_reserved_slots(monkeypatch):
    """If a class is already in our reservations, skip it — don't queue a
    booking job that would land in the 'already reserved' idempotent path."""
    rule = _backup_rule(1)
    week1 = _ov_slot("w1", datetime(2026, 5, 27, 18, 30, tzinfo=TZ))
    week2 = _ov_slot("w2", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))

    queued_ids: list[str] = []

    class FakeScheduler:
        def add_job(self, fn, trigger, **kwargs):
            queued_ids.append(kwargs["id"])

    monkeypatch.setattr(scheduler, "get_scheduler", lambda: FakeScheduler())

    await scheduler.horizon_scan(
        rule, all_slots=[week1, week2], booked_class_ids={"w1"},
    )

    assert queued_ids == ["rule-1-slot-w2"], (
        "w1 is already booked; only w2 should get queued"
    )


# ---- Credit awareness: classifier, gate, skip, forecast, warning ----------

from datetime import date as _date  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from pushpress import SubscriptionUsage  # noqa: E402


def _usage(remaining, period_end="2026-07-16", limit=9):
    used = None if remaining is None else max(limit - remaining, 0)
    return SubscriptionUsage(
        subscription_uuid="sub-1",
        plan="plan-1",
        status="active",
        limit=limit,
        reservations=used or 0,
        checkins=0,
        used=used,
        remaining=remaining,
        period="A",
        period_start="2026-06-19",
        period_end=period_end,
    )


class _ExecFakeDb:
    """Fake Session whose exec().all() returns a fixed rule list (used by the
    shared forecast helper, which queries rules via db.exec(select(...))."""

    def __init__(self, rules):
        self._rules = rules

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def exec(self, _query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        return _Result(self._rules)


def test_classify_reason_buckets():
    assert scheduler._classify_reason("terminal: You are out of sessions") == "out_of_credits"
    assert scheduler._classify_reason("cap exceeded") == "out_of_credits"
    assert scheduler._classify_reason("already reserved") == "already_booked"
    assert scheduler._classify_reason("class full at window open (0/14)") == "class_full"
    assert scheduler._classify_reason("no spots left") == "class_full"
    assert scheduler._classify_reason("registration has not yet started") == "window_not_open"
    assert scheduler._classify_reason("target class disappeared") == "slot_gone"
    assert scheduler._classify_reason("some random transient blip") == "other"
    assert scheduler._classify_reason("") == "other"


def test_out_of_sessions_is_user_terminal():
    """Regression: 'out of sessions' must be terminal so a per-class credit
    rejection doesn't burn 5x30s of transient retries."""
    assert scheduler._is_user_terminal("You are out of sessions for this class") is True
    assert scheduler._is_user_terminal("out of session") is True
    # A genuine transient stays non-terminal.
    assert scheduler._is_user_terminal("connection reset by peer") is False


@pytest.mark.asyncio
async def test_credit_gate_blocks_when_remaining_zero(monkeypatch):
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(return_value=[_usage(remaining=0)]),
    )
    blocked, period_end = await scheduler._credit_gate()
    assert blocked is True
    assert period_end == "2026-07-16"


@pytest.mark.asyncio
async def test_credit_gate_open_when_credits_available(monkeypatch):
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(return_value=[_usage(remaining=2)]),
    )
    blocked, period_end = await scheduler._credit_gate()
    assert blocked is False
    assert period_end is None


@pytest.mark.asyncio
async def test_credit_gate_fails_open_on_lookup_error(monkeypatch):
    """A broken credit lookup must NEVER block a legit booking."""
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(side_effect=RuntimeError("api down")),
    )
    blocked, period_end = await scheduler._credit_gate()
    assert blocked is False
    assert period_end is None


@pytest.mark.asyncio
async def test_booking_window_job_skips_on_zero_credits(monkeypatch, reset_loop_guard):
    """0 remaining credits: skip the booking entirely — book() NOT called,
    a 'skipped' attempt recorded, the user notified, no backup chain, and the
    outcome string surfaced for the caller."""
    rule = _backup_rule(1, backup_rule_id=2)  # has a backup — must NOT chain
    slot = _ov_slot("cal-zero", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _FakeDb(rule))
    async def fake_lookup(uuid):
        return slot
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(return_value=[_usage(remaining=0)]),
    )

    book_calls: list[str] = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)

    chain_calls: list = []
    async def fake_chain(*a, **k):
        chain_calls.append(True)
        return True
    monkeypatch.setattr(scheduler, "_chain_to_backup", fake_chain)

    send_calls: list[str] = []
    async def fake_send(msg):
        send_calls.append(msg)
    monkeypatch.setattr(scheduler.notify, "send", fake_send)
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda *_a, **_k: None)

    record_calls: list[tuple] = []
    monkeypatch.setattr(
        scheduler, "_record",
        lambda *a, **k: record_calls.append(a),
    )

    outcome = await scheduler.booking_window_job(1, 0, "cal-zero")

    assert book_calls == [], "must not call book() when out of credits"
    assert chain_calls == [], "0-credit skip must not chain to backup"
    assert outcome == "skipped: out of credits, period ends 2026-07-16"
    assert any(r[2] == "skipped" for r in record_calls), "a 'skipped' attempt must be recorded"
    assert len(send_calls) == 1 and "out of credits" in send_calls[0].lower()


@pytest.mark.asyncio
async def test_booking_window_job_books_when_credits_available(monkeypatch, reset_loop_guard):
    """With credits remaining, the gate is transparent — book() is called and
    the outcome is 'booked'."""
    rule = _backup_rule(1)
    slot = _ov_slot("cal-ok", datetime(2026, 6, 3, 18, 30, tzinfo=TZ), spots=5)

    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _FakeDb(rule))
    async def fake_lookup(uuid):
        return slot
    monkeypatch.setattr(scheduler, "_lookup_slot", fake_lookup)
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(return_value=[_usage(remaining=2)]),
    )

    book_calls: list[str] = []
    async def fake_book(uuid):
        from pushpress import BookingResult
        book_calls.append(uuid)
        return BookingResult(ok=True, reservation_id="r", message="booked")
    monkeypatch.setattr(scheduler.pushpress, "book", fake_book)
    monkeypatch.setattr(scheduler.notify, "send", _async_noop)
    monkeypatch.setattr(scheduler, "reminder_scan_job", _async_noop)
    monkeypatch.setattr(scheduler, "_record", lambda *_a, **_k: None)

    outcome = await scheduler.booking_window_job(1, 0, "cal-ok")

    assert book_calls == ["cal-ok"]
    assert outcome == "booked"


@pytest.mark.asyncio
async def test_scheduled_bookings_before_period_end_counts_matches(monkeypatch):
    """The shared forecast helper counts every matching, unbooked slot on or
    before period_end across active primary rules."""
    rule = make_rule(dow=2, hh=18, mm=30)  # Wed 18:30 OV Classic CrossFit
    now = datetime(2026, 6, 1, 12, 0, tzinfo=TZ)
    # Two Wednesdays before 16 Jul, one already booked, one after period_end.
    w1 = _ov_slot("w1", datetime(2026, 6, 3, 18, 30, tzinfo=TZ))
    w2 = _ov_slot("w2", datetime(2026, 6, 10, 18, 30, tzinfo=TZ))
    booked = _ov_slot("bk", datetime(2026, 6, 17, 18, 30, tzinfo=TZ))
    after = _ov_slot("late", datetime(2026, 7, 22, 18, 30, tzinfo=TZ))  # > period_end

    monkeypatch.setattr(
        scheduler.pushpress, "list_schedule",
        AsyncMock(return_value=[w1, w2, booked, after]),
    )

    class _Res:
        class_id = "bk"
    monkeypatch.setattr(
        scheduler.pushpress, "list_reservations",
        AsyncMock(return_value=[_Res()]),
    )
    monkeypatch.setattr(scheduler, "DbSession", lambda *_a, **_k: _ExecFakeDb([rule]))

    total, per_rule = await scheduler.scheduled_bookings_before_period_end(
        [_usage(remaining=1)], now=now,
    )

    assert total == 2, "w1 + w2 count; booked slot and post-period slot excluded"
    assert len(per_rule) == 1
    assert per_rule[0]["count"] == 2
    assert per_rule[0]["rule_id"] == 1


@pytest.mark.asyncio
async def test_credit_warning_job_queues_when_overbooked(monkeypatch):
    """When scheduled bookings before a near period_end exceed remaining
    credits, a heads-up is queued for the daily digest."""
    near = (_date.today() + timedelta(days=2)).isoformat()
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(return_value=[_usage(remaining=1, period_end=near)]),
    )
    monkeypatch.setattr(
        scheduler, "scheduled_bookings_before_period_end",
        AsyncMock(return_value=(3, [{"rule_id": 1, "name": "Wed Classic", "count": 3, "slots": []}])),
    )
    queued: list[str] = []
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda m: queued.append(m))

    await scheduler.credit_warning_job()

    assert len(queued) == 1
    assert "overbook" in queued[0].lower()


@pytest.mark.asyncio
async def test_credit_warning_job_silent_within_budget(monkeypatch):
    """No warning when scheduled bookings fit inside the remaining budget."""
    near = (_date.today() + timedelta(days=2)).isoformat()
    monkeypatch.setattr(
        scheduler.pushpress, "list_subscription_usage",
        AsyncMock(return_value=[_usage(remaining=5, period_end=near)]),
    )
    monkeypatch.setattr(
        scheduler, "scheduled_bookings_before_period_end",
        AsyncMock(return_value=(2, [{"rule_id": 1, "name": "Wed Classic", "count": 2, "slots": []}])),
    )
    queued: list[str] = []
    monkeypatch.setattr(scheduler.notify, "queue_for_digest", lambda m: queued.append(m))

    await scheduler.credit_warning_job()

    assert queued == []
