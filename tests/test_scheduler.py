from datetime import datetime, time
from zoneinfo import ZoneInfo

import scheduler
from models import AutomationRule

TZ = ZoneInfo("Europe/Amsterdam")


def make_rule(dow: int, hh: int, mm: int = 0, lead: int = 24) -> AutomationRule:
    return AutomationRule(
        id=1,
        class_name_pattern="Strength",
        day_of_week=dow,
        time_of_day=time(hh, mm),
        lead_time_hours=lead,
        enabled=True,
    )


def test_next_class_datetime_future_same_week():
    # Wed 12:00 in May 2026 — find next Friday 17:00
    now = datetime(2026, 5, 13, 12, 0, tzinfo=TZ)  # Wed
    rule = make_rule(dow=4, hh=17)  # Fri 17:00
    dt = scheduler.next_class_datetime(rule, now=now)
    assert dt.weekday() == 4
    assert dt.hour == 17
    assert dt > now


def test_next_class_datetime_rolls_to_next_week_if_passed():
    # Already past today's slot -> next week's
    now = datetime(2026, 5, 13, 18, 0, tzinfo=TZ)  # Wed 18:00
    rule = make_rule(dow=2, hh=17)  # Wed 17:00 (already past)
    dt = scheduler.next_class_datetime(rule, now=now)
    assert dt.weekday() == 2
    assert (dt - now).days >= 6


def test_next_window_open_uses_lead_time():
    now = datetime(2026, 5, 13, 12, 0, tzinfo=TZ)
    rule = make_rule(dow=4, hh=17, lead=24)
    class_dt = scheduler.next_class_datetime(rule, now=now)
    win = scheduler.next_window_open(rule, now=now)
    assert (class_dt - win).total_seconds() == 24 * 3600
