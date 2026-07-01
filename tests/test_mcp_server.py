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
