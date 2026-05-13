from datetime import datetime, time
from pathlib import Path

from sqlmodel import Field, SQLModel, create_engine

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
    lead_time_hours: int = 336  # 14 days
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BookingAttempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rule_id: int | None = Field(default=None, foreign_key="automationrule.id")
    fired_at: datetime = Field(default_factory=datetime.utcnow)
    target_class: str
    status: str  # "success" | "failure" | "retry"
    message: str = ""


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
