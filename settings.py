import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_username: str = "admin"
    app_password: str = "change-me"
    secret_key: str = "dev-secret-change-me"
    log_level: str = "INFO"
    port: int = 2009
    database_url: str = "sqlite:///./data/domcity.db"

    # Public base URL of this app, used as the OAuth issuer for the MCP server.
    # Locally this is http://localhost:2009; in production set it to the exact
    # public HTTPS domain Coolify serves (e.g. https://domcity.example.com).
    mcp_base_url: str = "http://localhost:2009"

    fernet_key: str = ""

    # PushPress login. The app POSTs to /v2/auth/login on startup and again
    # whenever the cached JWT is within 7 days of expiry, so you never touch
    # tokens manually.
    pushpress_email: str = ""
    pushpress_password: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    tz: str = "Europe/Amsterdam"


settings = Settings()
os.environ.setdefault("TZ", settings.tz)
