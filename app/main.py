import logging
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.bot import handle_update, notify_saved_users, poll_telegram
from app.config import settings
from app.db import engine, init_db

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
redis: Redis | None = None
scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
poll_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, poll_task
    await init_db()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    if settings.telegram_bot_token:
        scheduler.add_job(
            notify_saved_users,
            "interval",
            hours=6,
            next_run_time=datetime.now(ZoneInfo("Asia/Tashkent")),
            args=[redis],
            id="frost_alerts",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        if not settings.webhook_url:
            poll_task = asyncio.create_task(poll_telegram(redis), name="telegram-long-poll")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if poll_task:
        poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await poll_task
    if redis:
        await redis.aclose()
    await engine.dispose()


app = FastAPI(title="Zamorozki Bot", version="1.0.0", lifespan=lifespan)


async def process_update(payload: dict) -> None:
    try:
        await handle_update(payload, redis)
    except Exception as exc:
        log.error("Telegram update handling failed (%s)", type(exc).__name__)


@app.get("/health")
async def health() -> JSONResponse:
    checks: dict[str, bool] = {}
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception as exc:
        log.warning("Health check failed for PostgreSQL (%s)", type(exc).__name__)
        checks["postgres"] = False

    try:
        checks["redis"] = bool(redis and await redis.ping())
    except Exception as exc:
        log.warning("Health check failed for Redis (%s)", type(exc).__name__)
        checks["redis"] = False

    if settings.webhook_url:
        checks["telegram"] = bool(settings.telegram_bot_token and settings.webhook_secret)
    else:
        checks["telegram"] = bool(
            settings.telegram_bot_token and poll_task and not poll_task.done()
        )

    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "service": "zamorozki-bot", "checks": checks},
    )


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if not settings.webhook_secret or x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    payload = await request.json()
    background_tasks.add_task(process_update, payload)
    return {"ok": True}
