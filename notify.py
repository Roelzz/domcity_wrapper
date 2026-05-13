from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from settings import settings

TELEGRAM_API = "https://api.telegram.org"

# Daily digest buffer. Non-critical events (poll-started, token-refresh
# failures, etc.) get queued here and sent as a single Telegram message
# by scheduler.daily_digest_job. Lost on container restart — that's fine,
# everything user-actionable is sent immediately.
_digest_buffer: list[tuple[datetime, str]] = []


async def send(message: str) -> None:
    """Send an immediate Telegram message. Use only for user-actionable
    events (bookings, reminders, lost-class outcomes). Everything else
    should go through queue_for_digest()."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("Telegram not configured, skipping notification: {}", message)
        return
    url = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
    except Exception as e:
        logger.error("Telegram send failed: {}", e)


def queue_for_digest(line: str) -> None:
    """Buffer a line for the daily digest. Cheap (no I/O, no dedup).
    Use for informational events that don't need an immediate Telegram."""
    _digest_buffer.append((datetime.now(ZoneInfo(settings.tz)), line))
    logger.debug("Queued for digest: {}", line)


async def flush_digest() -> int:
    """Send any buffered digest lines as one Telegram message. Returns the
    number of lines sent (0 if buffer was empty — no Telegram fired)."""
    if not _digest_buffer:
        return 0
    lines = list(_digest_buffer)
    _digest_buffer.clear()
    bullets = "\n".join(f"• {ts.strftime('%H:%M')} {text}" for ts, text in lines)
    await send(f"📋 Domcity Planner — last 24h\n\n{bullets}")
    return len(lines)


def digest_buffer_size() -> int:
    return len(_digest_buffer)
