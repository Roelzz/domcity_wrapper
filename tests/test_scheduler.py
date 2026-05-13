from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import scheduler
from models import AutomationRule
from pushpress import ClassSlot

TZ = ZoneInfo("Europe/Amsterdam")


def make_rule(dow: int, hh: int, mm: int = 0) -> AutomationRule:
    return AutomationRule(
        id=1,
        name="Test rule",
        location="Overste den Oudenlaan 9",
        class_category="Classic CrossFit",
        day_of_week=dow,
        time_of_day=time(hh, mm),
        enabled=True,
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
