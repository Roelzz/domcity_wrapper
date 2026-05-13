import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_password: str = "change-me"
    secret_key: str = "dev-secret-change-me"
    log_level: str = "INFO"
    port: int = 2009
    database_url: str = "sqlite:///./data/domcity.db"

    fernet_key: str = ""

    pushpress_base_url: str = "https://members.pushpress.com"
    pushpress_email: str = ""
    pushpress_password: str = ""
    booking_window_hours: int = 24

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    tz: str = "Europe/Amsterdam"


settings = Settings()
os.environ.setdefault("TZ", settings.tz)
