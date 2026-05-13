"""Tests for the GraphQL PushPress client.

Real shapes captured from a session against api.pushpress.com. Auth is mocked
with a fake JWT (decoded base64-only, never verified) — the real token is
HS256-signed by the server and not derivable client-side.
"""

import base64
import json
from datetime import date

import httpx
import pytest
import respx

import pushpress
import settings as settings_module

FAKE_JWT = "header." + base64.urlsafe_b64encode(
    json.dumps(
        {
            "clientUuid": "client_test",
            "sub": "usr_test",
            "exp": 9999999999,
            "iat": 1700000000,
        }
    ).encode()
).rstrip(b"=").decode() + ".sig"


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    """Bypass real login by priming the in-process token and stubbing ensure_token."""
    monkeypatch.setattr(settings_module.settings, "pushpress_email", "test@example.com")
    monkeypatch.setattr(settings_module.settings, "pushpress_password", "test-pw")
    pushpress._set_active_token(FAKE_JWT)
    pushpress._tenant = None

    async def _noop() -> str:
        return FAKE_JWT
    monkeypatch.setattr(pushpress, "ensure_token", _noop)
    monkeypatch.setattr(pushpress, "force_refresh", _noop)
    yield
    pushpress._active_token = ""
    pushpress._active_expiry = None
    pushpress._tenant = None


def test_decode_jwt_extracts_claims():
    assert pushpress.token_client_uuid() == "client_test"
    assert pushpress.token_user_uuid() == "usr_test"
    exp = pushpress.token_expiry()
    assert exp is not None and exp.year >= 2286  # 9999999999 is year 2286


@respx.mock
@pytest.mark.asyncio
async def test_list_schedule_parses_real_shape():
    respx.post(pushpress.GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "classes": [
                        {
                            "uuid": "cal-abc",
                            "title": "OV | Classic CrossFit",
                            "spotsAvailable": 9,
                            "attendanceCap": 12,
                            "registrationStartOffset": -20160,
                            "registrationEndOffset": 0,
                            "startTime": "2026-05-14T06:00:00.000Z",
                            "endTime": "2026-05-14T07:00:00.000Z",
                            "mainCoach": {
                                "firstName": "Leilani",
                                "lastName": "Tison",
                                "__typename": "Profile",
                            },
                            "__typename": "Class",
                        }
                    ],
                    "__typename": "Query",
                }
            },
        )
    )
    slots = await pushpress.list_schedule(date(2026, 5, 14), date(2026, 5, 14))
    assert len(slots) == 1
    s = slots[0]
    assert s.id == "cal-abc"
    assert s.name == "OV | Classic CrossFit"
    assert s.spots_available == 9
    assert s.spots_total == 12
    assert s.instructor == "Leilani Tison"
    assert s.registration_start_offset_min == -20160


@respx.mock
@pytest.mark.asyncio
async def test_list_reservations_filters_cancelled():
    respx.post(pushpress.GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "reservations": [
                        {
                            "uuid": "reg-1",
                            "reservationTitle": "Classic CrossFit",
                            "calendarItemUuid": "cal-1",
                            "isActive": True,
                            "isCancelled": False,
                            "rawStartTime": "2026-05-14T06:00:00+00:00",
                            "rawEndTime": "2026-05-14T07:00:00+00:00",
                            "rawStatus": "registered",
                            "calendarItem": {"mainCoach": None, "__typename": "Class"},
                            "__typename": "Reservation",
                        },
                        {
                            "uuid": "reg-2",
                            "reservationTitle": "Cancelled one",
                            "calendarItemUuid": "cal-2",
                            "isActive": False,
                            "isCancelled": True,
                            "rawStartTime": "2026-05-14T08:00:00+00:00",
                            "rawEndTime": "2026-05-14T09:00:00+00:00",
                            "rawStatus": "cancelled",
                            "calendarItem": {"mainCoach": None, "__typename": "Class"},
                            "__typename": "Reservation",
                        },
                    ],
                    "__typename": "Query",
                }
            },
        )
    )
    items = await pushpress.list_reservations()
    assert len(items) == 1
    assert items[0].id == "reg-1"


@respx.mock
@pytest.mark.asyncio
async def test_book_uses_tenant_uuids():
    # First call -> profile lookup (lazy tenant load)
    # Then mutation. respx side_effect handles ordering.
    profile_resp = httpx.Response(
        200,
        json={
            "data": {
                "profile": {
                    "clientUserUuid": "cuu-test",
                    "userUuid": "usr_test",
                    "clientUuid": "client_test",
                    "firstName": "Test",
                    "lastName": "User",
                    "subscriptions": [
                        {
                            "subscriptionUuid": "sub_active",
                            "status": "active",
                            "active": True,
                            "plan": "p1",
                            "__typename": "Subscription",
                        }
                    ],
                    "__typename": "Profile",
                },
                "__typename": "Query",
            }
        },
    )
    book_resp = httpx.Response(
        200,
        json={
            "data": {
                "createReservation": {"uuid": "reg-new", "__typename": "Registration"},
                "__typename": "Mutation",
            }
        },
    )
    respx.post(pushpress.GRAPHQL_URL).mock(side_effect=[profile_resp, book_resp])
    result = await pushpress.book("cal-xyz")
    assert result.ok
    assert result.reservation_id == "reg-new"


@respx.mock
@pytest.mark.asyncio
async def test_book_returns_error_on_graphql_error():
    profile_resp = httpx.Response(
        200,
        json={
            "data": {
                "profile": {
                    "clientUserUuid": "cuu",
                    "userUuid": "u",
                    "clientUuid": "c",
                    "firstName": "T",
                    "lastName": "U",
                    "subscriptions": [
                        {
                            "subscriptionUuid": "sub",
                            "status": "active",
                            "active": True,
                            "plan": "p",
                            "__typename": "Subscription",
                        }
                    ],
                    "__typename": "Profile",
                }
            }
        },
    )
    err_resp = httpx.Response(
        200, json={"errors": [{"message": "Class full"}], "data": None}
    )
    respx.post(pushpress.GRAPHQL_URL).mock(side_effect=[profile_resp, err_resp])
    result = await pushpress.book("cal-xyz")
    assert not result.ok
    assert "Class full" in result.message


@respx.mock
@pytest.mark.asyncio
async def test_cancel_returns_true_on_success():
    respx.post(pushpress.GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "cancelReservation": {"uuid": "reg-1", "__typename": "Registration"}
                }
            },
        )
    )
    assert await pushpress.cancel("reg-1") is True


@respx.mock
@pytest.mark.asyncio
async def test_no_creds_raises(monkeypatch):
    monkeypatch.setattr(pushpress.settings, "pushpress_email", "")
    monkeypatch.setattr(pushpress.settings, "pushpress_password", "")
    with pytest.raises(RuntimeError, match="PUSHPRESS_EMAIL"):
        await pushpress.list_schedule(date(2026, 5, 14), date(2026, 5, 14))


@respx.mock
@pytest.mark.asyncio
async def test_login_parses_access_token():
    """Hit the real login endpoint shape with a mocked response."""
    respx.post(pushpress.LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"accessToken": FAKE_JWT, "refreshToken": "rt"})
    )
    token, expiry = await pushpress.login("a@b.c", "pw")
    assert token == FAKE_JWT
    assert expiry.year >= 2286  # exp=9999999999
