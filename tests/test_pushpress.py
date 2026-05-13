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
def fake_token(request, monkeypatch):
    """Bypass real login by priming the in-process token and stubbing ensure_token.
    Tests marked with `no_token_mock` opt out so they can exercise the real
    token lifecycle (force_refresh, _gql 401 handling, etc.)."""
    monkeypatch.setattr(settings_module.settings, "pushpress_email", "test@example.com")
    monkeypatch.setattr(settings_module.settings, "pushpress_password", "test-pw")
    pushpress._set_active_token(FAKE_JWT)
    pushpress._tenant = None

    if "no_token_mock" not in request.keywords:
        async def _noop() -> str:
            return FAKE_JWT
        monkeypatch.setattr(pushpress, "ensure_token", _noop)
        monkeypatch.setattr(pushpress, "force_refresh", _noop)
    yield
    pushpress._active_token = ""
    pushpress._active_expiry = None
    pushpress._last_login_success_at = None
    pushpress._last_login_attempt_at = None
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


@pytest.mark.no_token_mock
@pytest.mark.asyncio
async def test_force_refresh_dedupes_concurrent_calls(monkeypatch, tmp_path):
    """Two concurrent force_refresh() calls must result in exactly ONE login()."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    # Reset state so the deduplication window starts fresh
    pushpress._active_token = ""
    pushpress._active_expiry = None
    pushpress._last_login_success_at = None
    pushpress._last_login_attempt_at = None

    call_count = 0
    fresh_expiry = datetime.now(UTC) + timedelta(days=60)

    async def fake_login(email, password):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # simulate a slow login
        return FAKE_JWT, fresh_expiry

    monkeypatch.setattr(pushpress, "login", fake_login)
    # Stop ensure_token / _refresh_locked from hitting the DB
    monkeypatch.setattr(pushpress, "_set_active_token", lambda t: setattr(pushpress, "_active_token", t) or setattr(pushpress, "_active_expiry", fresh_expiry))

    # Bypass the DB persistence step by mocking the imports inside _refresh_locked.
    # Simpler: monkeypatch _refresh_locked itself? No — we want to test it.
    # Use a fake DB session via a temp sqlite.
    from sqlmodel import SQLModel, create_engine
    test_engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("models.engine", test_engine)

    # Fire two concurrent refreshes
    results = await asyncio.gather(pushpress.force_refresh(), pushpress.force_refresh())
    assert call_count == 1, f"expected 1 login() call, got {call_count}"
    assert all(r == FAKE_JWT for r in results)


@pytest.mark.no_token_mock
@pytest.mark.asyncio
async def test_force_refresh_short_circuits_within_window(monkeypatch):
    """If a fresh login happened seconds ago, force_refresh returns the
    active token without calling login() again."""
    from datetime import UTC, datetime, timedelta

    pushpress._active_token = "existing-token"
    pushpress._active_expiry = datetime.now(UTC) + timedelta(days=60)
    pushpress._last_login_success_at = datetime.now(UTC)  # just now
    pushpress._last_login_attempt_at = datetime.now(UTC)

    login_calls = 0
    async def fake_login(*a, **k):
        nonlocal login_calls
        login_calls += 1
        return "should-not-be-called", datetime.now(UTC) + timedelta(days=60)
    monkeypatch.setattr(pushpress, "login", fake_login)

    token = await pushpress.force_refresh()
    assert token == "existing-token"
    assert login_calls == 0


@pytest.mark.no_token_mock
@pytest.mark.asyncio
async def test_gql_does_not_refresh_on_401_when_token_is_valid(monkeypatch):
    """A 401 from PushPress should NOT trigger a re-login if the cached token
    is still good — assume transient server issue, propagate the error."""
    from datetime import UTC, datetime, timedelta

    import respx

    pushpress._active_token = "still-good-token"
    pushpress._active_expiry = datetime.now(UTC) + timedelta(hours=1)
    pushpress._last_login_attempt_at = datetime.now(UTC) - timedelta(minutes=1)

    login_calls = 0
    async def fake_login(*a, **k):
        nonlocal login_calls
        login_calls += 1
        return "new-token", datetime.now(UTC) + timedelta(days=60)
    monkeypatch.setattr(pushpress, "login", fake_login)

    with respx.mock:
        respx.post(pushpress.GRAPHQL_URL).mock(return_value=httpx.Response(401, text="nope"))
        with pytest.raises(pushpress._GqlError):
            await pushpress._gql("query{x}", {})
    assert login_calls == 0, "must not trigger login when cached token is still valid"
