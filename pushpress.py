"""
PushPress members API client.

The members portal at members.pushpress.com is a Flutter SPA that talks to
a GraphQL backend at api.pushpress.com/v2/graph/graphql. Auth is a bearer
JWT (HS256, server-signed) with a ~60-day lifetime.

Tokens are minted by POSTing email+password to /v2/auth/login. The app
caches the JWT in SQLite (TokenCache singleton), serves it from memory on
each GraphQL call, and the scheduler runs a daily 03:00 cron to re-login
whenever the cached token has < 7 days left. A 401/403 on any GraphQL call
also triggers an inline refresh + retry.

clientUuid + userUuid are decoded from the JWT. clientUserUuid and the
active subscriptionUuid are fetched once via GetProfiles and cached.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time as time_mod
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel

from settings import settings

API_BASE = "https://api.pushpress.com"
GRAPHQL_URL = f"{API_BASE}/v2/graph/graphql"
LOGIN_URL = f"{API_BASE}/v2/auth/login"
DEFAULT_HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "origin": "https://members.pushpress.com",
    "referer": "https://members.pushpress.com/",
}

# CrossFit-class calendarSessionTypeId. The Flutter SPA uses 2 for classes.
CALENDAR_SESSION_TYPE_ID = 2


class ClassSlot(BaseModel):
    id: str  # calendarItemUuid
    name: str
    location: str = ""           # e.g. "Havenweg 6"
    location_code: str = ""      # e.g. "HW" (parsed from title prefix)
    category: str = ""           # e.g. "Classic CrossFit" (parsed from title)
    start: datetime
    end: datetime
    instructor: str | None = None
    spots_available: int | None = None
    spots_total: int | None = None
    booked: bool = False
    registration_start_offset_min: int | None = None  # minutes before start


class Reservation(BaseModel):
    id: str  # reservation uuid
    class_id: str
    class_name: str
    start: datetime
    end: datetime
    instructor: str | None = None
    cancellable: bool = True


class BookingResult(BaseModel):
    ok: bool
    reservation_id: str | None = None
    message: str = ""


# -------- JWT + token lifecycle ----------------------------------------------


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Not a JWT (expected 3 segments)")
    pad = "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + pad))


# In-process active token. Primed at startup from DB, refreshed by login().
_active_token: str = ""
_active_expiry: datetime | None = None
_token_lock = asyncio.Lock()


def _set_active_token(token: str) -> None:
    """Set the in-process token and parse its expiry. Also resets caches that
    depended on the previous token's tenant context."""
    global _active_token, _active_expiry, _tenant
    _active_token = token
    try:
        _active_expiry = datetime.fromtimestamp(
            _decode_jwt_payload(token)["exp"], tz=UTC
        )
    except Exception:
        _active_expiry = None
    _tenant = None  # force re-lookup of profile under new token


def active_token() -> str:
    return _active_token


def token_expiry() -> datetime | None:
    return _active_expiry


def token_user_uuid() -> str:
    if not _active_token:
        return ""
    return _decode_jwt_payload(_active_token).get("sub", "")


def token_client_uuid() -> str:
    if not _active_token:
        return ""
    return _decode_jwt_payload(_active_token).get("clientUuid", "")


async def login(email: str, password: str) -> tuple[str, datetime]:
    """Exchange email+password for a fresh access token. Returns (token, expiry).
    Does not persist — caller writes to DB."""
    if not email or not password:
        raise RuntimeError("PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD not set")
    client = _get_client()
    r = await client.post(
        LOGIN_URL,
        headers={
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://members.pushpress.com",
            "referer": "https://members.pushpress.com/",
        },
        json={"username": email, "password": password},
    )
    if r.status_code != 200:
        raise RuntimeError(f"PushPress login failed: HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    token = data.get("accessToken")
    if not token:
        raise RuntimeError(f"Login response missing accessToken: keys={list(data.keys())}")
    claims = _decode_jwt_payload(token)
    expiry = datetime.fromtimestamp(claims["exp"], tz=UTC)
    logger.info("PushPress login OK, token expires {}", expiry.isoformat())
    return token, expiry


async def ensure_token() -> str:
    """Ensure the in-process token is valid. Reads cache from DB on first call,
    refreshes via login() if expired or missing. Returns the active token."""
    if _active_token and _active_expiry and _active_expiry > datetime.now(UTC):
        return _active_token
    async with _token_lock:
        if _active_token and _active_expiry and _active_expiry > datetime.now(UTC):
            return _active_token
        # Try DB cache first
        from sqlmodel import Session as _Sess

        from models import TokenCache as _TC
        from models import engine as _engine
        cached = None
        with _Sess(_engine) as db:
            cached = db.get(_TC, 1)
        if cached:
            try:
                exp = datetime.fromtimestamp(
                    _decode_jwt_payload(cached.access_token)["exp"], tz=UTC
                )
            except Exception:
                exp = None
            if exp and exp > datetime.now(UTC) + timedelta(minutes=5):
                _set_active_token(cached.access_token)
                logger.info("Token loaded from DB cache, expires {}", exp.isoformat())
                return _active_token
        # Cache empty or expired -> log in fresh
        token, expiry = await login(settings.pushpress_email, settings.pushpress_password)
        _set_active_token(token)
        # Persist
        with _Sess(_engine) as db:
            row = db.get(_TC, 1)
            if row:
                row.access_token = token
                row.expires_at = expiry
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                db.add(row)
            else:
                db.add(_TC(id=1, access_token=token, expires_at=expiry))
            db.commit()
        return _active_token


async def force_refresh() -> str:
    """Drop the cache and re-login. Used by the daily cron + on 401 retries."""
    global _active_token, _active_expiry
    async with _token_lock:
        _active_token = ""
        _active_expiry = None
    return await ensure_token()


# -------- Client state -------------------------------------------------------


@dataclass
class TenantContext:
    client_uuid: str
    user_uuid: str
    client_user_uuid: str
    subscription_uuid: str


_tenant: TenantContext | None = None


async def get_tenant() -> TenantContext:
    """Lazy-load + cache the user's tenant context (uuids + active subscription)."""
    global _tenant
    if _tenant is not None:
        return _tenant
    if not settings.pushpress_email or not settings.pushpress_password:
        raise RuntimeError("PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD not set")
    client_uuid = token_client_uuid()
    user_uuid = token_user_uuid()
    profile = await _gql(_QUERY_PROFILE, {"clientUuid": client_uuid, "userUuid": user_uuid})
    p = profile["profile"]
    sub = _pick_active_subscription(p.get("subscriptions") or [])
    _tenant = TenantContext(
        client_uuid=client_uuid,
        user_uuid=user_uuid,
        client_user_uuid=p["clientUserUuid"],
        subscription_uuid=sub["subscriptionUuid"] if sub else "",
    )
    logger.info(
        "PushPress tenant loaded: client={}, user={}, sub={}",
        _tenant.client_uuid,
        _tenant.user_uuid,
        _tenant.subscription_uuid or "(none)",
    )
    return _tenant


def _pick_active_subscription(subs: list[dict]) -> dict | None:
    for s in subs:
        if s.get("active") and (s.get("status") == "active"):
            return s
    return subs[0] if subs else None


# -------- Public API ---------------------------------------------------------


# Per-day schedule cache (TTL 60s). The schedule is read on every page render
# and barely changes minute-to-minute; this turns repeat hits into in-memory
# lookups and lets us deduplicate concurrent fetches.
_SCHEDULE_TTL_SEC = 60
_schedule_cache: dict[str, tuple[float, list[ClassSlot]]] = {}
_schedule_locks: dict[str, asyncio.Lock] = {}

# Reservations cache (shorter TTL — these change immediately after a booking).
_RESERVATIONS_TTL_SEC = 15
_reservations_cache: tuple[float, list[Reservation]] | None = None
_reservations_lock = asyncio.Lock()


def _now() -> float:
    return time_mod.monotonic()


async def _get_day(day: date) -> list[ClassSlot]:
    key = day.isoformat()
    cached = _schedule_cache.get(key)
    if cached and _now() - cached[0] < _SCHEDULE_TTL_SEC:
        return cached[1]
    # Lock per day so concurrent requests for the same day collapse into one.
    lock = _schedule_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _schedule_cache.get(key)
        if cached and _now() - cached[0] < _SCHEDULE_TTL_SEC:
            return cached[1]
        data = await _gql(_QUERY_CLASSES, {"classDate": key})
        slots: list[ClassSlot] = []
        for c in data.get("classes") or []:
            try:
                slots.append(_to_slot(c))
            except Exception as e:
                logger.warning("Skip malformed class {}: {}", c.get("uuid"), e)
        _schedule_cache[key] = (_now(), slots)
        return slots


async def list_schedule(start: date, end: date) -> list[ClassSlot]:
    """Fetch classes in [start, end] (inclusive). Days fetched in parallel
    via asyncio.gather and cached in-memory for 60 seconds."""
    if not settings.pushpress_email or not settings.pushpress_password:
        raise RuntimeError("PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD not set")
    days: list[date] = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    results = await asyncio.gather(*[_get_day(d) for d in days])
    out: list[ClassSlot] = []
    for chunk in results:
        out.extend(chunk)
    return out


def invalidate_schedule_cache() -> None:
    """Called after a booking succeeds so subsequent reads pick up the change."""
    _schedule_cache.clear()


async def list_reservations() -> list[Reservation]:
    if not settings.pushpress_email or not settings.pushpress_password:
        raise RuntimeError("PUSHPRESS_EMAIL / PUSHPRESS_PASSWORD not set")
    global _reservations_cache
    if _reservations_cache and _now() - _reservations_cache[0] < _RESERVATIONS_TTL_SEC:
        return _reservations_cache[1]
    async with _reservations_lock:
        if _reservations_cache and _now() - _reservations_cache[0] < _RESERVATIONS_TTL_SEC:
            return _reservations_cache[1]
        data = await _gql(_QUERY_RESERVATIONS, {})
        out: list[Reservation] = []
        for r in data.get("reservations") or []:
            if r.get("isCancelled") or not r.get("isActive"):
                continue
            try:
                out.append(_to_reservation(r))
            except Exception as e:
                logger.warning("Skip malformed reservation {}: {}", r.get("uuid"), e)
        _reservations_cache = (_now(), out)
        return out


def invalidate_reservations_cache() -> None:
    global _reservations_cache
    _reservations_cache = None


async def book(calendar_item_uuid: str) -> BookingResult:
    tenant = await get_tenant()
    if not tenant.subscription_uuid:
        return BookingResult(ok=False, message="No active subscription found on this account")
    variables = {
        "clientUserUuid": tenant.client_user_uuid,
        "calendarItemUuid": calendar_item_uuid,
        "subscriptionUuid": tenant.subscription_uuid,
        "source": "domcity_planner",
    }
    try:
        data = await _gql(_MUTATION_BOOK, variables)
        uuid = (data.get("createReservation") or {}).get("uuid")
        if uuid:
            invalidate_reservations_cache()
            invalidate_schedule_cache()
            return BookingResult(ok=True, reservation_id=uuid, message="booked")
        return BookingResult(ok=False, message="createReservation returned no uuid")
    except _GqlError as e:
        return BookingResult(ok=False, message=str(e))


async def cancel(reservation_uuid: str) -> bool:
    try:
        data = await _gql(_MUTATION_CANCEL, {"reservationId": reservation_uuid})
        ok = bool((data.get("cancelReservation") or {}).get("uuid"))
        if ok:
            invalidate_reservations_cache()
            invalidate_schedule_cache()
        return ok
    except _GqlError as e:
        logger.warning("cancel failed: {}", e)
        return False


# -------- Internals ----------------------------------------------------------


class _GqlError(RuntimeError):
    pass


# Shared module-level client. Keeps the TLS/HTTP2 connection pool open across
# requests instead of paying handshake cost on every call. Closed on shutdown.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=20,
            http2=False,  # would need h2 dep — http/1.1 keepalive is plenty here
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = await ensure_token()
    body = {"operationName": None, "variables": variables, "query": query}
    client = _get_client()
    r = await client.post(
        GRAPHQL_URL,
        headers={**DEFAULT_HEADERS, "authorization": f"Bearer {token}"},
        json=body,
    )
    # If the token was revoked or rotated server-side, refresh once and retry.
    if r.status_code in (401, 403):
        logger.info("PushPress returned {} — refreshing token and retrying", r.status_code)
        token = await force_refresh()
        r = await client.post(
            GRAPHQL_URL,
            headers={**DEFAULT_HEADERS, "authorization": f"Bearer {token}"},
            json=body,
        )
    if r.status_code != 200:
        raise _GqlError(f"HTTP {r.status_code}: {r.text[:300]}")
    payload = r.json()
    if payload.get("errors"):
        msg = "; ".join(e.get("message", "") for e in payload["errors"])
        if "Token" in msg or "token" in msg:
            logger.info("GraphQL token error — refreshing and retrying once")
            token = await force_refresh()
            r = await client.post(
                GRAPHQL_URL,
                headers={**DEFAULT_HEADERS, "authorization": f"Bearer {token}"},
                json=body,
            )
            payload = r.json()
            if payload.get("errors"):
                msg2 = "; ".join(e.get("message", "") for e in payload["errors"])
                raise _GqlError(msg2)
        else:
            raise _GqlError(msg)
    return payload.get("data") or {}


def _parse_dt(v: Any) -> datetime:
    """Parse a PushPress datetime string.

    PushPress serialises class times with a `Z`/`+00:00` suffix, but the
    digits are actually the gym's local wall-clock time — NOT UTC. (Verified
    against booked classes that came back at the local-clock hour despite
    being tagged as +00:00.) We strip the misleading UTC suffix and
    re-attach the configured local zone so all comparisons + display work
    correctly.
    """
    from zoneinfo import ZoneInfo
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    else:
        raise ValueError(f"Cannot parse datetime: {v!r}")
    # Treat the wall-clock as local time at the gym.
    return dt.replace(tzinfo=ZoneInfo(settings.tz))


def _to_slot(c: dict) -> ClassSlot:
    coach = c.get("mainCoach") or {}
    coach_name = " ".join(x for x in [coach.get("firstName"), coach.get("lastName")] if x) or None
    location = (c.get("location") or {}).get("name") or ""
    title = c.get("title") or "Class"
    code, category = parse_title(title)
    return ClassSlot(
        id=c["uuid"],
        name=title,
        location=location,
        location_code=code,
        category=category,
        start=_parse_dt(c.get("startTime") or c.get("startDatetime")),
        end=_parse_dt(c.get("endTime") or c.get("endDatetime")),
        instructor=coach_name,
        spots_available=c.get("spotsAvailable"),
        spots_total=c.get("attendanceCap"),
        booked=False,  # cross-ref via reservations
        registration_start_offset_min=c.get("registrationStartOffset"),
    )


# Known category roots — extend as new ones appear. The parser picks the
# longest prefix that matches one of these against the post-pipe segment.
_KNOWN_CATEGORIES = (
    "Classic CrossFit",
    "Functional CrossFit",
    "Hyrox Open",
    "Open Gym",
    "Trial Class",
    "Trial class",
)


def parse_title(title: str) -> tuple[str, str]:
    """Return (location_code, category) parsed from a PushPress class title.

    Examples:
      "OV | Classic CrossFit"                  -> ("OV", "Classic CrossFit")
      "HW | Open Gym Back Hall 6"              -> ("HW", "Open Gym")
      "Trial Class | Kanaalweg 29c"            -> ("",   "Trial Class")
      "Trial class | CrossFit | Overste den…"  -> ("",   "Trial Class")
    """
    parts = [p.strip() for p in title.split("|")]
    if not parts:
        return "", ""
    head = parts[0]
    rest = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Trial classes don't have a location code prefix
    if head.lower().startswith("trial"):
        return "", "Trial Class"

    # Treat the first segment as the location code if it's short uppercase
    if len(head) <= 4 and head.isupper():
        code = head
        candidate = rest
    else:
        code = ""
        candidate = title  # whole title

    cat = ""
    for known in _KNOWN_CATEGORIES:
        if candidate.lower().startswith(known.lower()):
            cat = known.title() if known.islower() else known
            break
    if not cat:
        # fall back to first two words of candidate
        cat = " ".join(candidate.split()[:2]) or candidate
    return code, cat


def _to_reservation(r: dict) -> Reservation:
    ci = r.get("calendarItem") or {}
    coach = ci.get("mainCoach") or {}
    coach_name = " ".join(x for x in [coach.get("firstName"), coach.get("lastName")] if x) or None
    status = (r.get("rawStatus") or "").lower()
    cancellable = bool(r.get("isActive")) and not r.get("isCancelled") and status not in {"attended", "no_show"}
    return Reservation(
        id=r["uuid"],
        class_id=r.get("calendarItemUuid") or "",
        class_name=r.get("reservationTitle") or "Class",
        start=_parse_dt(r.get("rawStartTime") or r.get("reservationStart")),
        end=_parse_dt(r.get("rawEndTime") or r.get("reservationEnd")),
        instructor=coach_name,
        cancellable=cancellable,
    )


# -------- GraphQL queries ----------------------------------------------------

_QUERY_PROFILE = """
query GetProfiles($clientUuid: String!, $userUuid: String!) {
  profile: getProfile(getProfileInput: {clientUuid: $clientUuid, userUuid: $userUuid}) {
    clientUserUuid
    userUuid
    clientUuid
    firstName
    lastName
    subscriptions {
      subscriptionUuid
      status
      active
      plan
      __typename
    }
    __typename
  }
  __typename
}
"""

_QUERY_CLASSES = """
query GetClasses($classDate: Date!) {
  classes: getCalendarItems(getCalendarItemsInput: {startDate: $classDate, endDate: $classDate, calendarSessionTypeId: 2}) {
    uuid
    title
    attendanceCap
    spotsAvailable
    registrationStartOffset
    registrationEndOffset
    startTime: startDatetime
    endTime: endDatetime
    location { name __typename }
    mainCoach { firstName lastName __typename }
    __typename
  }
  __typename
}
"""

_QUERY_RESERVATIONS = """
query GetUpcomingReservations {
  reservations: getUpcomingReservations {
    uuid
    reservationTitle
    calendarItemUuid
    isActive
    isCancelled
    rawStartTime: reservationStart
    rawEndTime: reservationEnd
    rawStatus: status
    calendarItem {
      mainCoach { firstName lastName __typename }
      __typename
    }
    __typename
  }
  __typename
}
"""

_MUTATION_BOOK = """
mutation CreateReservation($clientUserUuid: String!, $calendarItemUuid: String!, $subscriptionUuid: String!, $source: String) {
  createReservation(createReservationInput: {clientUserUuid: $clientUserUuid, calendarItemUuid: $calendarItemUuid, subscriptionUuid: $subscriptionUuid, source: $source}) {
    uuid
    __typename
  }
  __typename
}
"""

_MUTATION_CANCEL = """
mutation CancelReservation($reservationId: String!) {
  cancelReservation(cancelReservationInput: {reservationId: $reservationId}) {
    uuid
    __typename
  }
  __typename
}
"""
