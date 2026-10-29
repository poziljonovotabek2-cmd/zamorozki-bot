import asyncio
import logging
from datetime import date

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import settings
from app.db import Session
from app.i18n import LANG_NAMES, TEXT, t
from app.models import User
from app.weather import archive, forecast, forecast_risks, geocode, history_summary, local_now, ordinal_date

log = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
DOC_URL = (
    ("https://open-meteo.com/en/docs", "https://open-meteo.com/en/docs/historical-weather-api")
)


async def telegram(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{API}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data.get("result", {})


async def poll_telegram(redis: Redis) -> None:
    """Use Telegram long polling when no public webhook URL is configured."""
    offset: int | None = None
    timeout = httpx.Timeout(45, connect=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            info_response = await client.get(f"{API}/getWebhookInfo")
            info_response.raise_for_status()
            info = info_response.json().get("result", {})
            if info.get("url"):
                log.error("A Telegram webhook is already configured; polling was not started")
                return
            me_response = await client.get(f"{API}/getMe")
            me_response.raise_for_status()
            me = me_response.json().get("result", {})
            log.info("Telegram long polling started for @%s", me.get("username", "bot"))
        except Exception as exc:
            log.error("Could not initialize Telegram long polling (%s)", type(exc).__name__)
            return

        while True:
            try:
                params: dict[str, object] = {"timeout": 30, "allowed_updates": '["message", "callback_query"]'}
                if offset is not None:
                    params["offset"] = offset
                response = await client.get(f"{API}/getUpdates", params=params)
                response.raise_for_status()
                result = response.json()
                if not result.get("ok"):
                    raise RuntimeError("Telegram getUpdates returned ok=false")
                for update in result.get("result", []):
                    try:
                        await handle_update(update, redis)
                    except Exception as exc:
                        log.error("Failed to process Telegram update %s (%s)", update.get("update_id"), type(exc).__name__)
                    offset = int(update["update_id"]) + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Telegram long polling request failed (%s); retrying shortly", type(exc).__name__)
                await asyncio.sleep(3)


def reply_markup(language: str) -> dict:
    keyboard = [[{"text": label} for label in row] for row in TEXT[language]["keyboard"]]
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": TEXT[language]["forecast"][:64],
    }


def link_markup(language: str) -> dict:
    labels = TEXT[language]["links"]
    return {"inline_keyboard": [[{"text": label, "url": url}] for (label, _), url in zip(labels, DOC_URL)]}


async def send(
    chat_id: int,
    text: str,
    language: str = "ru",
    *,
    ask_location: bool = False,
    include_links: bool = False,
) -> None:
    payload = {
        "chat_id": chat_id, "text": text,
        "reply_markup": reply_markup(language),
        "link_preview_options": {"is_disabled": True},
    }
    if ask_location:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": TEXT[language]["location_button"], "request_location": True}],
                *[[{"text": label} for label in row] for row in TEXT[language]["keyboard"]],
            ],
            "resize_keyboard": True, "is_persistent": True,
        }
    await telegram("sendMessage", payload)
    if include_links:
        await telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "📎 Open‑Meteo",
            "reply_markup": link_markup(language),
            "link_preview_options": {"is_disabled": True},
        })


async def language_picker(chat_id: int, language: str = "ru") -> None:
    await telegram("sendMessage", {
        "chat_id": chat_id, "text": t(language, "choose_language"),
        "reply_markup": {"inline_keyboard": [[
            {"text": LANG_NAMES[code], "callback_data": f"lang:{code}"} for code in ("ru", "uz", "en")
        ]]},
    })


def place_label(result: dict) -> str:
    pieces = [result.get("name"), result.get("admin1"), result.get("country")]
    return ", ".join(dict.fromkeys(piece for piece in pieces if piece))


async def user_or_create(session, telegram_id: int) -> tuple[User, bool]:
    user = await session.get(User, telegram_id)
    if user:
        return user, False
    user = User(telegram_id=telegram_id, language="ru")
    session.add(user)
    await session.commit()
    return user, True


async def set_language(callback: dict) -> None:
    callback_id = callback["id"]
    data = callback.get("data", "")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    telegram_id = callback.get("from", {}).get("id")
    if not telegram_id or not chat_id or not data.startswith("lang:"):
        await telegram("answerCallbackQuery", {"callback_query_id": callback_id})
        return
    language = data.partition(":")[2]
    if language not in LANG_NAMES:
        await telegram("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": t("ru", "unknown"),
            "show_alert": True,
        })
        return
    async with Session() as session:
        user, _ = await user_or_create(session, telegram_id)
        user.language = language
        await session.commit()
        has_place = user.latitude is not None
    await telegram("answerCallbackQuery", {"callback_query_id": callback_id, "text": t(language, "language_set")})
    await send(chat_id, t(language, "language_set") + "\n\n" + (t(language, "unknown") if has_place else t(language, "ask_location")), language, ask_location=not has_place)


async def save_coordinates(session, user: User, lat: float, lon: float, label: str) -> None:
    user.latitude, user.longitude, user.place_name = lat, lon, label[:200]
    await session.commit()


async def display_date(day: date, language: str) -> str:
    if language == "en":
        return day.strftime("%-d %B")
    months = {
        "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
        "uz": ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"],
    }
    return f"{day.day} {months[language][day.month - 1]}"


async def handle_forecast(chat_id: int, user: User, redis: Redis) -> None:
    language, place = user.language, user.place_name or ""
    try:
        data = await forecast(redis, user.latitude, user.longitude)
        risks = forecast_risks(data)
        if not risks:
            await send(chat_id, t(language, "forecast_clear", place=place), language)
            return
        items = []
        for risk in risks:
            day = date.fromisoformat(risk["date"])
            items.append(t(language, risk["severity"], place=place, date=await display_date(day, language), temp=f"{risk['minimum']:.1f}"))
        await send(chat_id, "\n\n".join(items), language)
    except Exception as exc:
        log.error("Forecast request failed (%s)", type(exc).__name__)
        await send(chat_id, t(language, "forecast_error"), language)


async def handle_history(chat_id: int, user: User, redis: Redis) -> None:
    language, place = user.language, user.place_name or ""
    try:
        forecast_data = await forecast(redis, user.latitude, user.longitude)
        place_today = local_now(forecast_data).date()
        data = await archive(redis, user.latitude, user.longitude, place_today)
        summary = history_summary(data)
        years = summary["years"]
        if not years:
            await send(chat_id, t(language, "history_error"), language)
            return
        spring_day = await display_date(ordinal_date(summary["spring"]["ordinal"]), language)
        autumn_day = await display_date(ordinal_date(summary["autumn"]["ordinal"]), language)
        spring_count, autumn_count = summary["spring"]["safe_count"], summary["autumn"]["safe_count"]
        spring_share, autumn_share = round(100 * spring_count / years), round(100 * autumn_count / years)
        lines = [
            t(language, "history_title", place=place, years=years),
            t(language, "spring", date=spring_day, share=spring_share, count=spring_count, years=years),
            t(language, "autumn", date=autumn_day, share=autumn_share, count=autumn_count, years=years),
        ]
        if years < 8:
            lines.append(t(language, "confidence_low", count=years))
        lines.append(t(language, "model_note"))
        await send(chat_id, "\n\n".join(lines), language)
    except Exception as exc:
        log.error("Archive calculation failed (%s)", type(exc).__name__)
        await send(chat_id, t(language, "history_error"), language)


async def handle_update(update: dict, redis: Redis) -> None:
    if update.get("callback_query"):
        await set_language(update["callback_query"])
        return
    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    telegram_id = message["from"]["id"]
    text = (message.get("text") or "").strip()
    async with Session() as session:
        user, is_new = await user_or_create(session, telegram_id)
        language = user.language if user.language in TEXT else "ru"
        if is_new or text.startswith("/start"):
            await send(chat_id, t(language, "welcome"), language)
            await language_picker(chat_id, language)
            return
        location = message.get("location")
        if location:
            lat, lon = float(location["latitude"]), float(location["longitude"])
            label = {"ru": "Метка на карте", "uz": "Xaritadagi nuqta", "en": "Map pin"}[language]
            await save_coordinates(session, user, lat, lon, label)
            await send(chat_id, t(language, "saved_place", place=label), language)
            return
        if text == "/language" or text == TEXT[language]["language"]:
            await language_picker(chat_id, language)
            return
        if text == "/help" or text == TEXT[language]["help_button"]:
            await send(chat_id, t(language, "help"), language, include_links=True)
            return
        if text == "/location" or text == TEXT[language]["change_place"]:
            await send(chat_id, t(language, "set_location"), language, ask_location=True)
            return
        if text == TEXT[language]["forecast"] or text == "/forecast":
            if user.latitude is None:
                await send(chat_id, t(language, "need_place"), language, ask_location=True)
            else:
                await handle_forecast(chat_id, user, redis)
            return
        if text == TEXT[language]["planting"] or text == "/planting":
            if user.latitude is None:
                await send(chat_id, t(language, "need_place"), language, ask_location=True)
            else:
                await handle_history(chat_id, user, redis)
            return
        if text.startswith("/"):
            await send(chat_id, t(language, "unknown"), language)
            return
        try:
            result = await geocode(text, language)
        except Exception as exc:
            log.error("Geocoding failed (%s)", type(exc).__name__)
            result = None
        if result:
            label = place_label(result)
            await save_coordinates(session, user, float(result["latitude"]), float(result["longitude"]), label)
            await send(chat_id, t(language, "saved_place", place=label), language)
        else:
            await send(chat_id, t(language, "not_found"), language, ask_location=True)


async def notify_saved_users(redis: Redis) -> None:
    """Background warning, deduplicated per user and local forecast event."""
    async with Session() as session:
        users = (await session.scalars(select(User).where(User.latitude.is_not(None), User.longitude.is_not(None)))).all()
        # Detach the small set so network calls don't hold a database connection open.
        targets = [(u.telegram_id, u.language, u.latitude, u.longitude, u.place_name) for u in users]
    for telegram_id, language, latitude, longitude, place in targets:
        try:
            data = await forecast(redis, latitude, longitude)
            for risk in forecast_risks(data):
                key = f"alert:{telegram_id}:{risk['date']}:{risk['severity']}"
                if await redis.set(key, "1", ex=72 * 60 * 60, nx=True):
                    day = date.fromisoformat(risk["date"])
                    message = t(language, risk["severity"], place=place, date=await display_date(day, language), temp=f"{risk['minimum']:.1f}")
                    await send(telegram_id, message, language)
        except Exception as exc:
            log.error("Scheduled forecast failed for user %s (%s)", telegram_id, type(exc).__name__)
