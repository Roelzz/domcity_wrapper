"""Tests for the MCP tool wrappers.

Uses FastMCP's in-memory ``Client`` transport, which talks to the ``mcp`` server
object directly and bypasses the OAuth/HTTP layer — so these exercise the tool
logic with ``pushpress`` and ``scheduler`` mocked. DB-backed tools use the temp
SQLite database configured in ``conftest.py``.
"""

from datetime import datetime, time
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlmodel import Session as DbSession
from sqlmodel import delete, select

import models
import pushpress
import scheduler
from mcp_server import mcp
from models import AutomationRule, engine
from pushpress import BookingResult, ClassSlot


@pytest.fixture(autouse=True)
def clean_db():
    """Ensure tables exist and start each test with no rules/attempts."""
    models.init_db()
    with DbSession(engine) as db:
        db.exec(delete(models.BookingAttempt))
        db.exec(delete(AutomationRule))
        db.commit()
    yield


def _slot(**over) -> ClassSlot:
    base = dict(
        id="cal-uuid-1",
        name="Classic CrossFit",
        location="Overste den Oudenlaan 9",
        location_code="OV",
        category="Classic CrossFit",
        start=datetime(2026, 5, 15, 17, 0),
        end=datetime(2026, 5, 15, 18, 0),
        instructor="Coach",
        spots_available=5,
        spots_total=12,
        booked=False,
        registration_start_offset_min=None,
    )
    base.update(over)
    return ClassSlot(**base)


async def test_get_schedule_returns_serialized_slots(monkeypatch):
    monkeypatch.setattr(pushpress, "list_schedule", AsyncMock(return_value=[_slot()]))
    async with Client(mcp) as client:
        result = await client.call_tool("get_schedule", {})
    assert isinstance(result.data, list)
    assert result.data[0]["id"] == "cal-uuid-1"
    assert result.data[0]["name"] == "Classic CrossFit"


async def test_get_schedule_bad_date_raises():
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_schedule", {"start_date": "not-a-date"})


async def test_get_session_credits_returns_serialized_usage(monkeypatch):
    usage = pushpress.SubscriptionUsage(
        subscription_uuid="sub-1",
        plan="plan-1",
        status="active",
        limit=9,
        reservations=3,
        checkins=5,
        used=8,
        remaining=1,
        period="A",
        period_start="2026-06-19",
        period_end="2026-07-16",
    )
    monkeypatch.setattr(
        pushpress, "list_subscription_usage", AsyncMock(return_value=[usage])
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_session_credits", {})
    assert isinstance(result.data, list)
    assert result.data[0]["subscription_uuid"] == "sub-1"
    assert result.data[0]["limit"] == 9
    assert result.data[0]["remaining"] == 1


async def test_book_class_schedules_reminders_on_success(monkeypatch):
    monkeypatch.setattr(
        pushpress,
        "book",
        AsyncMock(return_value=BookingResult(ok=True, reservation_id="res-1", message="booked")),
    )
    reminder = AsyncMock()
    monkeypatch.setattr(scheduler, "reminder_scan_job", reminder)

    async with Client(mcp) as client:
        result = await client.call_tool("book_class", {"calendar_item_uuid": "cal-uuid-1"})

    assert result.data["ok"] is True
    assert result.data["reservation_id"] == "res-1"
    reminder.assert_awaited_once()


async def test_create_automation_rule_persists_and_rearms(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(scheduler, "horizon_refresh_all", refresh)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_automation_rule",
            {
                "name": "Friday CrossFit",
                "location": "Overste den Oudenlaan 9",
                "class_category": "Classic CrossFit",
                "day_of_week": 4,
                "time_of_day": "17:00",
            },
        )

    assert result.data["name"] == "Friday CrossFit"
    assert result.data["day_of_week"] == 4
    assert result.data["time_of_day"] == "17:00"
    refresh.assert_awaited_once()

    with DbSession(engine) as db:
        rows = db.exec(select(AutomationRule)).all()
    assert len(rows) == 1
    assert rows[0].name == "Friday CrossFit"


async def test_create_automation_rule_rejects_bad_day(monkeypatch):
    monkeypatch.setattr(scheduler, "horizon_refresh_all", AsyncMock())
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "create_automation_rule",
                {
                    "name": "x",
                    "location": "y",
                    "class_category": "z",
                    "day_of_week": 9,
                    "time_of_day": "17:00",
                },
            )


async def test_list_automation_rules_returns_existing():
    with DbSession(engine) as db:
        db.add(
            AutomationRule(
                name="Existing",
                location="Overste den Oudenlaan 9",
                class_category="Classic CrossFit",
                day_of_week=2,
                time_of_day=time(17, 0),
                enabled=True,
            )
        )
        db.commit()

    async with Client(mcp) as client:
        result = await client.call_tool("list_automation_rules", {})

    assert len(result.data) == 1
    assert result.data[0]["name"] == "Existing"
    assert result.data[0]["day_name"] == "Wednesday"


async def test_toggle_unknown_rule_raises():
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("toggle_automation_rule", {"rule_id": 999, "enabled": False})


# --- Credit-aware insight/foresight tools ---------------------------------- #
def _usage(remaining, *, limit=9, period_end="2026-07-16"):
    used = max(limit - remaining, 0)
    return pushpress.SubscriptionUsage(
        subscription_uuid="sub-1",
        plan="unlimited-plan",
        status="active",
        limit=limit,
        reservations=used,
        checkins=0,
        used=used,
        remaining=remaining,
        period="A",
        period_start="2026-06-19",
        period_end=period_end,
    )


def _insert_rule_with_failures(reason_msg: str, n_fail: int = 3, n_success: int = 0) -> int:
    """Insert a rule plus terminal failure/success attempts; return the rule id."""
    with DbSession(engine) as db:
        rule = AutomationRule(
            name="OV Classic",
            location="Overste den Oudenlaan 9",
            class_category="Classic CrossFit",
            day_of_week=0,
            time_of_day=time(18, 30),
            enabled=True,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        rid = rule.id
        for _ in range(n_fail):
            db.add(models.BookingAttempt(
                rule_id=rid, target_class="OV Classic", status="failure", message=reason_msg
            ))
        for _ in range(n_success):
            db.add(models.BookingAttempt(
                rule_id=rid, target_class="OV Classic", status="success", message="booked"
            ))
        db.commit()
    return rid


async def test_get_stats_surfaces_credits_overbooked_and_health(monkeypatch):
    rid = _insert_rule_with_failures("You are out of sessions for this class", n_fail=3)
    monkeypatch.setattr(
        pushpress, "list_subscription_usage", AsyncMock(return_value=[_usage(1)])
    )
    # Isolate the forecast count: 2 scheduled vs 1 remaining -> overbooked.
    monkeypatch.setattr(
        scheduler,
        "scheduled_bookings_before_period_end",
        AsyncMock(return_value=(2, [{"rule_id": rid, "name": "OV Classic", "count": 2, "slots": []}])),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_stats", {})

    data = result.data
    assert data["credits"]["remaining"] == 1
    assert data["credits"]["limit"] == 9
    assert data["overbooked"] is True
    reasons = {r["reason"]: r["count"] for r in data["by_reason"]}
    assert reasons.get("out_of_credits") == 3
    rule_row = next(r for r in data["per_rule"] if r["rule_id"] == rid)
    assert rule_row["health"] == "credit_capped"
    assert rule_row["reasons"]["out_of_credits"] == 3
    assert rule_row["success_rate"] == 0.0


async def test_get_stats_fails_open_when_credits_unavailable(monkeypatch):
    _insert_rule_with_failures("class is full", n_fail=1)
    monkeypatch.setattr(
        pushpress,
        "list_subscription_usage",
        AsyncMock(side_effect=RuntimeError("creds unset")),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_stats", {})

    data = result.data
    assert data["credits"] is None
    assert data["overbooked"] is False
    # Rest of the stats are still populated.
    assert data["n_failure"] == 1
    assert {r["reason"] for r in data["by_reason"]} == {"class_full"}


async def test_forecast_overbooked_recommends_worst_rule(monkeypatch):
    rid = _insert_rule_with_failures("You are out of sessions for this class", n_fail=3)
    monkeypatch.setattr(
        pushpress, "list_subscription_usage", AsyncMock(return_value=[_usage(1)])
    )
    monkeypatch.setattr(
        scheduler,
        "scheduled_bookings_before_period_end",
        AsyncMock(return_value=(2, [{"rule_id": rid, "name": "OV Classic", "count": 2, "slots": []}])),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_automation_forecast", {})

    data = result.data
    assert data["overbooked"] is True
    assert data["scheduled_before_period_end"]["count"] == 2
    assert data["recommendation"] is not None
    assert data["recommendation"]["rule_id"] == rid
    assert "pause_automation_rule" in data["recommendation"]["action"]
    row = data["scheduled_before_period_end"]["per_rule"][0]
    assert row["health"] == "credit_capped"


async def test_forecast_on_track_has_no_recommendation(monkeypatch):
    rid = _insert_rule_with_failures("booked", n_fail=0, n_success=2)
    monkeypatch.setattr(
        pushpress, "list_subscription_usage", AsyncMock(return_value=[_usage(5)])
    )
    monkeypatch.setattr(
        scheduler,
        "scheduled_bookings_before_period_end",
        AsyncMock(return_value=(2, [{"rule_id": rid, "name": "OV Classic", "count": 2, "slots": []}])),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_automation_forecast", {})

    data = result.data
    assert data["overbooked"] is False
    assert data["recommendation"] is None
    assert "On track" in data["summary"]


async def test_forecast_fails_open_on_usage_error(monkeypatch):
    monkeypatch.setattr(
        pushpress,
        "list_subscription_usage",
        AsyncMock(side_effect=RuntimeError("creds unset")),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_automation_forecast", {})

    data = result.data
    assert data["credits"] is None
    assert data["overbooked"] is False
    assert data["scheduled_before_period_end"]["count"] == 0


async def test_fire_automation_rule_reports_credit_skip(monkeypatch):
    with DbSession(engine) as db:
        rule = AutomationRule(
            name="OV Classic",
            location="Overste den Oudenlaan 9",
            class_category="Classic CrossFit",
            day_of_week=0,
            time_of_day=time(18, 30),
            enabled=True,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        rid = rule.id

    monkeypatch.setattr(
        scheduler,
        "find_next_matching_slot",
        AsyncMock(return_value=(_slot(), None)),
    )
    monkeypatch.setattr(
        scheduler,
        "booking_window_job",
        AsyncMock(return_value="skipped: out of credits, period ends 2026-07-16"),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("fire_automation_rule", {"rule_id": rid})

    data = result.data
    assert data["ok"] is True
    assert data["booked"] is False
    assert data["outcome"].startswith("skipped: out of credits")
    assert data["targeted_class"] == "Classic CrossFit"
