import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    admin_chat_id: int | None
    max_token: str | None
    max_admin_id: int | None
    max_api_url: str | None
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str | None
    llm_reasoning_effort: str | None
    tz: ZoneInfo
    db_path: Path


def _int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def load_settings() -> Settings:
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip() or None,
        admin_chat_id=_int("ADMIN_CHAT_ID"),
        max_token=os.getenv("MAX_BOT_TOKEN", "").strip() or None,
        max_admin_id=_int("MAX_ADMIN_ID"),
        max_api_url=os.getenv("MAX_API_URL", "").strip() or None,
        llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip() or None,
        llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip() or None,
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        db_path=BASE_DIR / os.getenv("DB_PATH", "bookings.db"),
    )
