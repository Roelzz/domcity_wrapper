from datetime import datetime, time
from zoneinfo import ZoneInfo

import scheduler
from models import AutomationRule

TZ = ZoneInfo("Europe/Amsterdam")


def make_rule(dow: int, hh: int, mm: int = 0, lead: int = 336) -> AutomationRule:
    return AutomationRule(
        id=1,
        name="Test rule",
        location="Overste den Oudenlaan 9",
        class_category="Classic CrossFit",
        day_of_week=dow,
        time_of_day=time(hh, mm),
        lead_time_hours=lead,
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
    rule = make_rule(dow=2, hh=17)  # today 17:00, already past
    dt = scheduler.next_class_datetime(rule, now=now)
    assert dt.weekday() == 2
    assert (dt - now).days >= 6


def test_next_window_open_uses_lead_time():
    now = datetime(2026, 5, 13, 12, 0, tzinfo=TZ)
    rule = make_rule(dow=4, hh=17, lead=336)  # 14 days
    class_dt = scheduler.next_class_datetime(rule, now=now)
    win = scheduler.next_window_open(rule, now=now)
    assert (class_dt - win).total_seconds() == 336 * 3600


def test_match_slot_filters_by_location_and_category():
    from pushpress import ClassSlot

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
        ClassSlot(
            id="c", name="OV | Functional CrossFit", location="Overste den Oudenlaan 9",
            location_code="OV", category="Functional CrossFit",
            start=target, end=target,
        ),
    ]
    rule = make_rule(dow=3, hh=9)  # Thu 09:00, OV/Classic CrossFit
    match = scheduler._match_slot(slots, rule, target)
    assert match is not None
    assert match.id == "a"


def test_match_slot_returns_none_when_time_off():
    from pushpress import ClassSlot

    target = datetime(2026, 5, 14, 9, 0, tzinfo=TZ)
    far = datetime(2026, 5, 14, 14, 0, tzinfo=TZ)
    slots = [
        ClassSlot(
            id="a", name="OV | Classic CrossFit", location="Overste den Oudenlaan 9",
            location_code="OV", category="Classic CrossFit",
            start=far, end=far,
        ),
    ]
    rule = make_rule(dow=3, hh=9)
    assert scheduler._match_slot(slots, rule, target) is None
