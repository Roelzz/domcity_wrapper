"""Tests against the speculative endpoint shapes. Once a HAR is captured and
pushpress.py is updated, rewrite these fixtures to match real responses."""

from datetime import datetime

import httpx
import pytest
import respx

import pushpress


@pytest.mark.asyncio
@respx.mock
async def test_login_extracts_csrf_and_succeeds():
    base = "https://members.pushpress.com"
    respx.get(f"{base}/login").mock(
        return_value=httpx.Response(
            200, html='<meta name="csrf-token" content="abc123">'
        )
    )
    respx.post(f"{base}/login").mock(
        return_value=httpx.Response(200, html="<html>dashboard</html>")
    )
    session = await pushpress.login("a@b.c", "pw")
    try:
        assert session.csrf_token == "abc123"
    finally:
        await session.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_list_schedule_parses_speculative_shape():
    base = "https://members.pushpress.com"
    respx.get(f"{base}/login").mock(return_value=httpx.Response(200, html=""))
    respx.post(f"{base}/login").mock(return_value=httpx.Response(200, html=""))
    respx.get(f"{base}/api/schedule").mock(
        return_value=httpx.Response(
            200,
            json={
                "classes": [
                    {
                        "id": "42",
                        "name": "Strength",
                        "start": "2026-05-15T17:00:00",
                        "end": "2026-05-15T18:00:00",
                        "instructor": "Coach K",
                        "spots_available": 3,
                        "spots_total": 12,
                    }
                ]
            },
        )
    )
    session = await pushpress.login("a@b.c", "pw")
    try:
        slots = await pushpress.list_schedule(
            session, datetime(2026, 5, 15), datetime(2026, 5, 16)
        )
        assert len(slots) == 1
        assert slots[0].id == "42"
        assert slots[0].name == "Strength"
        assert slots[0].spots_available == 3
    finally:
        await session.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_book_returns_ok():
    base = "https://members.pushpress.com"
    respx.get(f"{base}/login").mock(return_value=httpx.Response(200, html=""))
    respx.post(f"{base}/login").mock(return_value=httpx.Response(200, html=""))
    respx.post(f"{base}/api/reservations").mock(
        return_value=httpx.Response(200, json={"id": "r-99"})
    )
    session = await pushpress.login("a@b.c", "pw")
    try:
        result = await pushpress.book(session, "42")
        assert result.ok
        assert result.reservation_id == "r-99"
    finally:
        await session.aclose()
