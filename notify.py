import httpx
from loguru import logger

from settings import settings

TELEGRAM_API = "https://api.telegram.org"


async def send(message: str) -> None:
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
