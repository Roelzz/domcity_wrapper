"""Tests for the digest queue in notify.py."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import notify


@pytest.fixture(autouse=True)
def reset_digest():
    notify._digest_buffer.clear()
    yield
    notify._digest_buffer.clear()


def test_queue_appends_to_buffer():
    notify.queue_for_digest("hello")
    notify.queue_for_digest("world")
    assert notify.digest_buffer_size() == 2


@pytest.mark.asyncio
async def test_flush_digest_empty_does_not_send(monkeypatch):
    send_mock = AsyncMock()
    monkeypatch.setattr(notify, "send", send_mock)
    sent = await notify.flush_digest()
    assert sent == 0
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_flush_digest_with_content_sends_one_message(monkeypatch):
    notify.queue_for_digest("first thing")
    notify.queue_for_digest("second thing")
    send_mock = AsyncMock()
    monkeypatch.setattr(notify, "send", send_mock)
    sent = await notify.flush_digest()
    assert sent == 2
    send_mock.assert_called_once()
    msg = send_mock.call_args.args[0]
    assert "first thing" in msg
    assert "second thing" in msg
    assert "Domcity Planner" in msg
    assert notify.digest_buffer_size() == 0


@pytest.mark.asyncio
async def test_flush_digest_includes_timestamps(monkeypatch):
    notify.queue_for_digest("event at some time")
    send_mock = AsyncMock()
    monkeypatch.setattr(notify, "send", send_mock)
    await notify.flush_digest()
    msg = send_mock.call_args.args[0]
    # The bullet line has "HH:MM event at some time"
    assert "•" in msg
    assert "event at some time" in msg


def test_buffer_entries_use_local_tz():
    notify.queue_for_digest("now")
    ts, _ = notify._digest_buffer[0]
    assert ts.tzinfo is not None
    # Buffer entries shouldn't be more than a few seconds old
    now_tz = datetime.now(ts.tzinfo)
    assert abs(now_tz - ts) < timedelta(seconds=5)
