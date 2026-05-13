"""
PushPress members API client.

The members portal at members.pushpress.com is a Flutter SPA that talks to
a GraphQL backend at api.pushpress.com/v2/graph/graphql. Auth is a bearer
JWT (HS256, server-signed) with a ~60-day lifetime. There is no programmatic
login endpoint exposed to members — the token is minted server-side after
form login in the SPA. To refresh, the user opens members.pushpress.com in a
browser, copies a fresh `Authorization: Bearer …` value from any GraphQL
request in DevTools, and updates PUSHPRESS_TOKEN in .env.

clientUuid + userUuid are decoded from the JWT. clientUserUuid and the
active subscriptionUuid are fetched once at startup via GetProfiles.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel

from settings import settings

GRAPHQL_URL = "https://api.pushpress.com/v2/graph/graphql"
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


# -------- JWT helpers --------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Not a JWT (expected 3 segments)")
    pad = "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + pad))


def token_expiry() -> datetime | None:
    if not settings.pushpress_token:
        return None
    try:
        claims = _decode_jwt_payload(settings.pushpress_token)
        return datetime.fromtimestamp(claims["exp"], tz=UTC)
    except Exception as e:
        logger.warning("Could not decode token expiry: {}", e)
        return None


def token_user_uuid() -> str:
    return _decode_jwt_payload(settings.pushpress_token).get("sub", "")


def token_client_uuid() -> str:
    return _decode_jwt_payload(settings.pushpress_token).get("clientUuid", "")


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
    if not settings.pushpress_token:
        raise RuntimeError("PUSHPRESS_TOKEN not set")
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


async def list_schedule(start: date, end: date) -> list[ClassSlot]:
    """Fetch classes in [start, end] (inclusive). Calls per-day to keep query simple."""
    if not settings.pushpress_token:
        raise RuntimeError("PUSHPRESS_TOKEN not set")
    out: list[ClassSlot] = []
    day = start
    while day <= end:
        data = await _gql(_QUERY_CLASSES, {"classDate": day.isoformat()})
        for c in data.get("classes") or []:
            try:
                out.append(_to_slot(c))
            except Exception as e:
                logger.warning("Skip malformed class {}: {}", c.get("uuid"), e)
        day += timedelta(days=1)
    return out


async def list_reservations() -> list[Reservation]:
    if not settings.pushpress_token:
        raise RuntimeError("PUSHPRESS_TOKEN not set")
    data = await _gql(_QUERY_RESERVATIONS, {})
    out: list[Reservation] = []
    for r in data.get("reservations") or []:
        if r.get("isCancelled") or not r.get("isActive"):
            continue
        try:
            out.append(_to_reservation(r))
        except Exception as e:
            logger.warning("Skip malformed reservation {}: {}", r.get("uuid"), e)
    return out


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
            return BookingResult(ok=True, reservation_id=uuid, message="booked")
        return BookingResult(ok=False, message="createReservation returned no uuid")
    except _GqlError as e:
        return BookingResult(ok=False, message=str(e))


async def cancel(reservation_uuid: str) -> bool:
    try:
        data = await _gql(_MUTATION_CANCEL, {"reservationId": reservation_uuid})
        return bool((data.get("cancelReservation") or {}).get("uuid"))
    except _GqlError as e:
        logger.warning("cancel failed: {}", e)
        return False


# -------- Internals ----------------------------------------------------------


class _GqlError(RuntimeError):
    pass


async def _gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    headers = {
        **DEFAULT_HEADERS,
        "authorization": f"Bearer {settings.pushpress_token}",
    }
    body = {"operationName": None, "variables": variables, "query": query}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(GRAPHQL_URL, headers=headers, json=body)
    if r.status_code != 200:
        raise _GqlError(f"HTTP {r.status_code}: {r.text[:300]}")
    payload = r.json()
    if payload.get("errors"):
        msg = "; ".join(e.get("message", "") for e in payload["errors"])
        raise _GqlError(msg)
    return payload.get("data") or {}


def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    raise ValueError(f"Cannot parse datetime: {v!r}")


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
