from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    webhook_secret: str = ""
    webhook_url: str = ""
    database_url: str = "postgresql+asyncpg://frost:frost-local-only@localhost:5432/frostbot"
    redis_url: str = "redis://localhost:6379/0"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
