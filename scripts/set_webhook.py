import asyncio

import httpx

from app.config import settings


async def main() -> None:
    if not settings.telegram_bot_token or not settings.webhook_secret or not settings.webhook_url:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET and WEBHOOK_URL in .env first")
    url = settings.webhook_url.rstrip("/") + "/telegram/webhook"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
            json={"url": url, "secret_token": settings.webhook_secret, "allowed_updates": ["message", "callback_query"]},
        )
        try:
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPError as exc:
            raise SystemExit(f"Telegram webhook request failed ({type(exc).__name__}); URL and token hidden") from None
        if not result.get("ok"):
            raise SystemExit("Telegram rejected the webhook configuration; check the URL and secret")
        print("Webhook registered successfully")


if __name__ == "__main__":
    asyncio.run(main())
