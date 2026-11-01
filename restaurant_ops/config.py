"""Central settings (pydantic-settings). Import the `settings` singleton everywhere."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration; every field can be overridden by env var or `.env`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---
    SEED: int = 42
    DATA_DIR: Path = PROJECT_ROOT / "data"
    DB_PATH: Path = PROJECT_ROOT / "data" / "restaurant.db"
    MODELS_DIR: Path = PROJECT_ROOT / "data" / "models"
    CHECKPOINT_DB: Path = PROJECT_ROOT / "data" / "checkpoints.db"

    # --- calendar ---
    # First simulated day; history = HISTORY_DAYS days ending SIM_START-1. Any weekday works.
    # Unset (default): read from the seeded DB (meta.sim_start, written at seed time); with no DB yet, today.
    # Seeding uses today unless SIM_START is set in the env / .env, so history always ends on yesterday's date.
    SIM_START: date | None = None
    SIM_START_SOURCE: str = ""  # where SIM_START came from: "env" | "db" | "today" (filled in by the validator)
    HISTORY_DAYS: int = 365
    DEMO_DAYS: int = 7

    # --- restaurant ---
    SEATS: int = 80
    MEAL_PERIODS: list[str] = ["lunch", "dinner", "late"]
    SEAT_TURNS: dict[str, float] = {"lunch": 1.5, "dinner": 2.0, "late": 0.6}
    STATIONS: list[str] = ["grill", "saute", "fry", "garde_manger", "pastry"]
    PAYDAY_DAYS: list[int] = [15, -1]  # day of month; -1 = last day of month

    # --- thresholds ---
    APPROVAL_PO_THRESHOLD: float = 1500.0
    DAILY_BUDGET: float = 4000.0
    MAX_AGENT_STEPS: int = 8
    MAX_SUPERVISOR_STEPS: int = 3
    TRAIN_MAX_MEMORY_MB: int = 4096  # forecast training aborts when process RSS exceeds this; 0 = no limit

    # --- LLM ---
    LLM_MODE: str = ""  # "live" | "mock"; empty -> live if the chosen provider has an API key
    LLM_PROVIDER: str = "meta"  # "meta" | "openai" | "anthropic" | "ollama"
    LLM_MODEL: str = ""  # optional override: model name, or "provider:model" (e.g. "openai:gpt-4.1")

    # Meta Llama API (OpenAI-compatible endpoint)
    META_API_KEY: str = Field(default="", validation_alias=AliasChoices("META_API_KEY", "MODEL_API_KEY", "LLAMA_API_KEY"))
    META_BASE_URL: str = "https://api.meta.ai/v1"
    META_MODEL: str = "muse-spark-1.3"
    META_REASONING_EFFORT: str = ""  # optional reasoning_effort (e.g. "low" for faster cycles); empty = API default

    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""  # empty = api.openai.com
    OPENAI_MODEL: str = "gpt-4.1"

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5-5"

    OLLAMA_MODEL: str = "llama3.1"

    # --- observability (optional) ---
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "restaurant-ops"
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"

    @model_validator(mode="after")
    def _resolve(self) -> "Settings":
        """Resolve LLM_MODE default, export LangSmith env, and make sure data dirs exist."""
        if ":" in self.LLM_MODEL:
            self.LLM_PROVIDER, self.LLM_MODEL = self.LLM_MODEL.split(":", 1)
        self.LLM_PROVIDER = self.LLM_PROVIDER.strip().lower()
        if self.LLM_PROVIDER not in ("meta", "openai", "anthropic", "ollama"):
            raise ValueError(f"LLM_PROVIDER must be meta|openai|anthropic|ollama, got {self.LLM_PROVIDER!r}")
        if self.LLM_MODE not in ("live", "mock"):
            self.LLM_MODE = "live" if self.provider_api_key or self.LLM_PROVIDER == "ollama" else "mock"
        if self.ANTHROPIC_API_KEY:
            os.environ.setdefault("ANTHROPIC_API_KEY", self.ANTHROPIC_API_KEY)
        if self.OPENAI_API_KEY:
            os.environ.setdefault("OPENAI_API_KEY", self.OPENAI_API_KEY)
        if self.LANGSMITH_TRACING and self.LANGSMITH_API_KEY:
            os.environ.setdefault("LANGSMITH_TRACING", "true")
            os.environ.setdefault("LANGSMITH_API_KEY", self.LANGSMITH_API_KEY)
            os.environ.setdefault("LANGSMITH_PROJECT", self.LANGSMITH_PROJECT)
            os.environ.setdefault("LANGSMITH_ENDPOINT", self.LANGSMITH_ENDPOINT)
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.SIM_START is not None:
            self.SIM_START_SOURCE = "env"
        else:
            found = sim_start_from_db(self.DB_PATH)
            self.SIM_START = found or date.today()
            self.SIM_START_SOURCE = "db" if found else "today"
        return self

    @property
    def provider_api_key(self) -> str:
        """API key for the selected LLM provider ('' for ollama or when unset)."""
        return {
            "meta": self.META_API_KEY,
            "openai": self.OPENAI_API_KEY,
            "anthropic": self.ANTHROPIC_API_KEY,
        }.get(self.LLM_PROVIDER, "")

    @property
    def model_name(self) -> str:
        """Model for the selected provider: LLM_MODEL override, else the provider default."""
        return self.LLM_MODEL or {
            "meta": self.META_MODEL,
            "openai": self.OPENAI_MODEL,
            "anthropic": self.ANTHROPIC_MODEL,
            "ollama": self.OLLAMA_MODEL,
        }[self.LLM_PROVIDER]

    @property
    def history_start(self) -> date:
        """First day of the synthetic history."""
        from datetime import timedelta

        return self.SIM_START - timedelta(days=self.HISTORY_DAYS)

    def seat_cap(self, meal_period: str) -> int:
        """Max covers for a meal period = seats x turns."""
        return int(self.SEATS * self.SEAT_TURNS[meal_period])


def sim_start_from_db(db_path: "str | Path") -> date | None:
    """The first simulated day a DB was built for: meta.sim_start, else (older DBs) derived from its data.

    Seeded DB without meta: the day after the last history day in `covers`. A run DB without meta: its first
    kpi_daily day (the simulator writes covers for simulated days, so covers would point past the start).
    """
    import sqlite3
    from datetime import timedelta

    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        def one(sql: str):
            try:
                row = conn.execute(sql).fetchone()
                return row[0] if row else None
            except sqlite3.Error:
                return None

        v = one("SELECT value FROM meta WHERE key='sim_start'")
        if v:
            return date.fromisoformat(str(v)[:10])
        v = one("SELECT MIN(date) FROM kpi_daily")
        if v:
            return date.fromisoformat(str(v)[:10])
        v = one("SELECT MAX(date) FROM covers")
        if v:
            return date.fromisoformat(str(v)[:10]) + timedelta(days=1)
        return None
    finally:
        conn.close()


settings = Settings()
