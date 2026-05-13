from datetime import date, datetime, time
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, text

from settings import settings

DB_PATH = Path(settings.database_url.replace("sqlite:///", ""))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(settings.database_url, echo=False)


class AutomationRule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    location: str
    class_category: str
    day_of_week: int  # 0=Mon ... 6=Sun
    time_of_day: time
    enabled: bool = True
    # If set, the rule is paused for all classes ON OR BEFORE this date.
    # Cleared (set to None) to resume. Lets the user skip a holiday without
    # toggling individual rules manually.
    paused_until: date | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BookingAttempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rule_id: int | None = Field(default=None, foreign_key="automationrule.id")
    fired_at: datetime = Field(default_factory=datetime.utcnow)
    target_class: str
    status: str  # "success" | "failure" | "retry"
    message: str = ""


class TokenCache(SQLModel, table=True):
    """Singleton row (id=1) caching the active PushPress JWT and its expiry.
    Refreshed by logging in with email+password whenever <7 days remain."""
    id: int | None = Field(default=1, primary_key=True)
    access_token: str
    expires_at: datetime
    updated_at: datetime = Field(default_factory=datetime.utcnow)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_paused_until_column()


def _ensure_paused_until_column() -> None:
    """Lightweight 'migration' for the paused_until column on AutomationRule.
    SQLite-only; safe to run on every startup. Pre-existing DBs (without the
    column) get ALTERed; new DBs already have it from create_all."""
    with Session(engine) as db:
        cols = db.exec(text("PRAGMA table_info(automationrule)")).all()
        names = {row[1] for row in cols}
        if "paused_until" not in names:
            db.exec(text("ALTER TABLE automationrule ADD COLUMN paused_until DATE"))
            db.commit()
