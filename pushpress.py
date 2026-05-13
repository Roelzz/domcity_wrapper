"""
PushPress members portal client.

NOTE: PushPress publishes no member-level API. The endpoint paths and payload
shapes below are speculative scaffolding. Before deploying, capture a HAR file
from members.pushpress.com (DevTools -> Network -> save as HAR), then update
the constants in the ENDPOINTS block and the parser bodies below to match the
real responses. See docs/endpoints.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel

from settings import settings

# -------- ENDPOINTS (update once HAR is captured) ----------------------------
LOGIN_PATH = "/login"
SCHEDULE_PATH = "/api/schedule"
RESERVATIONS_PATH = "/api/reservations"
BOOK_PATH = "/api/reservations"
CANCEL_PATH = "/api/reservations/{reservation_id}"
# -----------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}


class ClassSlot(BaseModel):
    id: str
    name: str
    start: datetime
    end: datetime
    instructor: str | None = None
    spots_available: int | None = None
    spots_total: int | None = None
    booked: bool = False


class Reservation(BaseModel):
    id: str
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


@dataclass
class Session:
    client: httpx.AsyncClient
    csrf_token: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.client.aclose()


async def login(email: str, password: str) -> Session:
    client = httpx.AsyncClient(
        base_url=settings.pushpress_base_url,
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
        timeout=20,
    )
    # NOTE: real flow likely needs a GET to fetch a CSRF token first.
    # Once HAR is captured, parse it from the login page HTML or a cookie.
    get_resp = await client.get(LOGIN_PATH)
    csrf = _extract_csrf(get_resp.text)

    payload: dict[str, Any] = {"email": email, "password": password}
    if csrf:
        payload["_token"] = csrf

    resp = await client.post(LOGIN_PATH, data=payload)
    # Refine this check once the real HAR is captured — successful login
    # typically redirects to /dashboard or sets an auth cookie. Until then,
    # only treat HTTP error codes as failures.
    if resp.status_code >= 400:
        await client.aclose()
        raise RuntimeError(f"PushPress login failed: {resp.status_code} {resp.text[:200]}")

    logger.info("PushPress login OK as {}", email)
    return Session(client=client, csrf_token=csrf, cookies=dict(client.cookies))


def _extract_csrf(html: str) -> str | None:
    # Laravel apps commonly embed: <meta name="csrf-token" content="...">
    import re

    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    return None


async def list_schedule(session: Session, start: datetime, end: datetime) -> list[ClassSlot]:
    params = {"from": start.date().isoformat(), "to": end.date().isoformat()}
    r = await session.client.get(SCHEDULE_PATH, params=params)
    r.raise_for_status()
    return _parse_schedule(r.json())


async def list_reservations(session: Session) -> list[Reservation]:
    r = await session.client.get(RESERVATIONS_PATH)
    r.raise_for_status()
    return _parse_reservations(r.json())


async def book(session: Session, slot_id: str) -> BookingResult:
    headers = _csrf_header(session)
    r = await session.client.post(BOOK_PATH, json={"class_id": slot_id}, headers=headers)
    if r.status_code >= 400:
        return BookingResult(ok=False, message=f"{r.status_code}: {r.text[:200]}")
    data = r.json() if r.text else {}
    return BookingResult(ok=True, reservation_id=str(data.get("id", "")), message="booked")


async def cancel(session: Session, reservation_id: str) -> bool:
    headers = _csrf_header(session)
    r = await session.client.delete(
        CANCEL_PATH.format(reservation_id=reservation_id), headers=headers
    )
    return r.status_code < 400


def _csrf_header(session: Session) -> dict[str, str]:
    return {"X-CSRF-TOKEN": session.csrf_token} if session.csrf_token else {}


# -------- Parsers (rewrite once HAR is captured) -----------------------------


def _parse_schedule(data: Any) -> list[ClassSlot]:
    # Speculative shape: { "classes": [ {...} ] } or a bare list
    items = data.get("classes", data) if isinstance(data, dict) else data
    out: list[ClassSlot] = []
    for it in items or []:
        try:
            out.append(
                ClassSlot(
                    id=str(it.get("id")),
                    name=it.get("name", "Class"),
                    start=_dt(it.get("start") or it.get("starts_at")),
                    end=_dt(it.get("end") or it.get("ends_at")),
                    instructor=it.get("instructor") or it.get("coach"),
                    spots_available=it.get("spots_available") or it.get("available"),
                    spots_total=it.get("spots_total") or it.get("capacity"),
                    booked=bool(it.get("booked")),
                )
            )
        except Exception as e:
            logger.warning("Skipping malformed schedule item {}: {}", it, e)
    return out


def _parse_reservations(data: Any) -> list[Reservation]:
    items = data.get("reservations", data) if isinstance(data, dict) else data
    out: list[Reservation] = []
    for it in items or []:
        try:
            out.append(
                Reservation(
                    id=str(it.get("id")),
                    class_id=str(it.get("class_id") or it.get("classId") or ""),
                    class_name=it.get("name") or it.get("class_name") or "Class",
                    start=_dt(it.get("start") or it.get("starts_at")),
                    end=_dt(it.get("end") or it.get("ends_at")),
                    instructor=it.get("instructor") or it.get("coach"),
                    cancellable=bool(it.get("cancellable", True)),
                )
            )
        except Exception as e:
            logger.warning("Skipping malformed reservation {}: {}", it, e)
    return out


def _dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    raise ValueError(f"Cannot parse datetime: {v!r}")
