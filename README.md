# «Заморозки» — Telegram-бот

FastAPI Telegram bot with PostgreSQL storage, Redis cache, Russian / Uzbek / English messages, Open‑Meteo forecast and historical advice. It uses long polling by default or a webhook when a public HTTPS URL is configured.

## Start locally

1. Set `TELEGRAM_BOT_TOKEN` in `.env`. This project is configured for your running Postgres container on Docker network `docker_pg_network`.
2. Run `docker compose up --build -d`. This starts the bot and Redis and connects the bot to your existing Postgres. With no public URL configured, it uses Telegram long polling. The API listens on `http://localhost:8001`; pgAdmin remains at `http://localhost:8080`.
3. For webhook mode, set a public HTTPS `WEBHOOK_URL` and `WEBHOOK_SECRET` in `.env`, restart with `docker compose up -d`, then register it with `docker compose exec bot python -m scripts.set_webhook`.
4. In pgAdmin, connect to database `zamorozki_bot` as user `zamorozki_bot` to inspect the bot tables.

The Compose file joins the existing external Docker network, where Postgres is reachable as `postgres`. For another Postgres instance, update `DATABASE_URL` in `.env`.

## Bot behavior

- A persistent keyboard offers forecast, planting dates, location, language and Help. Open‑Meteo documentation buttons appear only when the user opens Help or sends `/help`. Location and language are stored by Telegram user ID in PostgreSQL.
- The location can be sent once as a Telegram location pin or typed as a city. Open‑Meteo geocoding resolves city names.
- The forecast checks the next 24–48 hours. A minimum of 0°C or lower is a freeze warning; +0.1…+2°C is a ground-frost watch. Suggestions include covering beds, draining exposed pipes and bringing seedlings inside.
- A background job checks saved locations every six hours and sends at most one alert per location/date/severity.
- Planting advice uses ten complete calendar years of hourly archive data. It groups hourly samples by Open‑Meteo's local timestamps and counts each year's last spring freeze and first autumn freeze. It gives a conservative 90% date and reports the actual historical share and sample size.
- Open‑Meteo archive data can be incomplete for recent days; calculations ignore missing dates. If fewer than eight years have usable observations, the bot says that confidence is low.
- Archived days with fewer than 18 hourly readings are skipped; each season-year needs at least 88 spring days and 87 autumn days to enter the date estimate.
- Redis caches forecasts for 20 minutes and archive calculations for 30 days.

## Freeze definition and limits

The archive's event is **hourly temperature at 2 m <= 0°C**, aggregated to each local calendar day. This is reproducible across years and matches a meteorological air-frost definition. It does not measure leaf or soil temperature: clear, still nights can cool plants at ground level below the 2 m reading. Therefore the live forecast uses +2°C as a precautionary watch threshold. Weather grids are estimates, not a thermometer at the dacha; microclimate, elevation and forecast error can change the result. The 90% statistic describes the selected historical sample, not a guarantee.

## Links

- [Open‑Meteo forecast documentation](https://open-meteo.com/en/docs)
- [Open‑Meteo historical weather API](https://open-meteo.com/en/docs/historical-weather-api)

No API key is required for the Open‑Meteo endpoints used here. Check Open‑Meteo's current terms and attribution requirements before public/commercial deployment.
