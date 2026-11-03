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
    STATIONS: list[str] = ["grill", "saute", "cold", "pastry"]
    PAYDAY_DAYS: list[int] = [15, -1]  # day of month; -1 = last day of month

    # --- thresholds ---
    APPROVAL_PO_THRESHOLD: float = 1500.0
    DAILY_BUDGET: float = 4000.0
    MAX_AGENT_STEPS: int = 8
    MAX_SUPERVISOR_STEPS: int = 3
    TRAIN_MAX_MEMORY_MB: int = 4096  # forecast training aborts when process RSS exceeds this; 0 = no limit

    # --- real-time service (restaurant_ops/live) ---
    EVENT_BUS: str = "auto"  # "auto" (Redis Streams if reachable, else in-process) | "redis" | "memory"
    REDIS_URL: str = "redis://localhost:6379/0"
    LIVE_DIR: Path = PROJECT_ROOT / "data" / "live"
    LIVE_PERIOD: str = "dinner"  # meal period a live service runs
    LIVE_PO_APPROVAL_THRESHOLD: float = 300.0  # unplanned in-service POs above this need the manager
    LIVE_DISCARD_APPROVAL_VALUE: float = 50.0  # discards worth more than this need the manager
    APPROVAL_TIMEOUT_SIM_MIN: float = 20.0  # service minutes (paused clock = frozen) before the safe default applies
    LIVE_HOST: str = "127.0.0.1"
    LIVE_PORT: int = 8000

    # --- live agent tunables ---
    COURSE_GAP_MIN: float = 12.0          # mains fire this long after starters
    SLAMMED_STREAK_MIN: int = 4           # minutes slammed before the Order agent asks to throttle online orders
    CALM_STREAK_MIN: int = 10             # minutes calm before online orders reopen
    COMP_LATE_MIN: float = 10.0           # online order this late past its P90 quote -> comp proposal
    LOW_DEBOUNCE_MIN: float = 30.0        # stock.low per ingredient at most this often (unless stock halves)
    EXPIRY_MIN_VALUE: float = 20.0        # lots worth less than this are not announced as expiring
    EXPIRY_ALERT_DAYS: int = 1            # lead time of lot.expiring alerts (days before expiry)
    MAX_SPECIALS: int = 2                 # active + pending specials
    FORECAST_MISS_PCT: float = 15.0
    FORECAST_MISS_Z: float = 1.5
    NEGOTIATE_MIN_LINE: float = 150.0     # emergency PO lines worth at least this are negotiated
    NEGOTIATE_TARGET: float = 0.95        # ask for 5% off list

    # --- prep & staffing (6th agent) ---
    COOK_HOURLY_RATE: float = 28.0
    SURGE_LABOR_BUDGET: float = 250.0     # extra labour the agent may spend per service (call-ins)
    SURGE_SHIFT_HOURS: float = 3.0
    SURGE_WAIT_MIN: float = 12.0          # station wait that counts as "behind"
    SURGE_STREAK_MIN: int = 4             # minutes behind before asking to call a cook in
    CALL_IN_DELAY_MIN: float = 20.0       # a called-in cook arrives after this
    MAX_EXTRA_COOKS: int = 1              # per station
    RELEASE_CALM_MIN: int = 30            # minutes calm before an extra cook is released
    PREP_REVIEW_EVERY_MIN: int = 10
    PREP_COOKS: int = 1                   # prep cook doing batch mise en place during service
    SCRATCH_PENALTY: float = 1.3          # cook time x this when nothing is prepped

    # --- front of house ---
    OVERBOOK_FACTOR: float = 1.1          # booked covers in a 90-min window may reach seats x this
    NO_SHOW_GRACE_MIN: float = 15.0
    TAX_RATE: float = 0.08875
    DISCOUNT_MAX_PCT: float = 30.0
    DELIVERY_DRIVERS: int = 3
    DELIVERY_ZONES: dict[str, dict] = {"A": {"label": "under 2 km", "fee": 3.0, "drive_min": 8.0},
                                       "B": {"label": "2-5 km", "fee": 5.0, "drive_min": 15.0},
                                       "C": {"label": "5-8 km", "fee": 8.0, "drive_min": 24.0}}
    DELIVERY_SHARE: float = 0.35          # simulated online orders that are deliveries
    LOYALTY_POINTS_PER_DOLLAR: float = 1.0
    LOYALTY_TIERS: dict[str, int] = {"Silver": 500, "Gold": 1500}

    # --- food safety ---
    TEMP_LOG_EVERY_MIN: int = 30
    TEMP_LIMITS: dict[str, list[float]] = {"walk_in": [0.0, 5.0], "freezer": [-30.0, -15.0], "hot_holding": [63.0, 90.0]}
    DONATION_MIN_VALUE: float = 10.0

    # --- identity (named users; roles admin > manager > server, kitchen) ---
    AUTH_ENABLED: bool = True
    SESSION_HOURS: float = 12.0
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = ""          # first-run admin; empty = a random password written to data/FIRST_RUN_ADMIN.txt
    DEMO_USERS: bool = True           # also create manager/server/kitchen demo users (password "<role>-demo"); false in production
    COOKIE_SECURE: bool = False       # set true behind HTTPS
    LOGIN_MAX_FAILURES: int = 5       # per username per LOGIN_LOCK_MIN minutes
    LOGIN_LOCK_MIN: float = 5.0

    # --- money & ordering edges ---
    PAYMENT_PROCESSOR: str = "simulated"   # simulated | stripe
    STRIPE_SECRET_KEY: str = ""
    PAYMENT_TIMEOUT_S: float = 15.0
    CURRENCY: str = "usd"
    OUTBOX_DIR: Path = PROJECT_ROOT / "data" / "outbox"   # PO PDFs / e-mails when SMTP is not configured
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "orders@bistro80.example"
    SMTP_STARTTLS: bool = True
    PRINTER_MODE: str = "file"        # file | tcp | off   (kitchen chits, one printer per station)
    PRINTER_DIR: Path = PROJECT_ROOT / "data" / "printer"
    PRINTER_HOST: str = ""            # tcp: an ESC/POS printer (port 9100); per-station override PRINTER_HOST_<STATION>
    PRINTER_PORT: int = 9100
    KDS_LATE_MIN: float = 20.0        # a KDS ticket older than this turns red

    # --- operations ---
    BACKUP_DIR: Path = PROJECT_ROOT / "data" / "backups"
    BACKUP_KEEP: int = 14
    EXPORT_DIR: Path = PROJECT_ROOT / "data" / "exports"
    SNAPSHOT_KEEP: int = 3            # rotated service snapshots

    # --- LLM pilot guard ---
    LLM_TIMEOUT_S: float = 12.0
    LLM_MAX_CALLS_PER_SERVICE: int = 150
    LLM_MAX_COST_PER_SERVICE: float = 2.0      # USD
    LLM_PRICE_IN_PER_MTOK: float = 3.0         # USD per million input tokens (set for your model)
    LLM_PRICE_OUT_PER_MTOK: float = 15.0
    LLM_BREAKER_FAILURES: int = 3              # consecutive failures/timeouts -> rule-based for the rest of the service

    # --- operations ---
    LOG_JSON: bool = False
    LOG_LEVEL: str = "INFO"
    LIVE_RETENTION_DAYS: int = 14         # delete live service DBs older than this ...
    LIVE_KEEP_LAST: int = 20              # ... but always keep the newest N
    RESUME_ON_START: bool = True          # the server resumes an interrupted service instead of starting fresh
    SNAPSHOT_EVERY_MIN: int = 5           # service state is saved this often (and on every decision)

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


# settings that were removed or renamed: still set in an environment or .env, they would be silently ignored
OBSOLETE = {
    "MANAGER_TOKEN": "role tokens were replaced by named users: sign in, and manage accounts in Setup > Users",
    "SERVER_TOKEN": "role tokens were replaced by named users: sign in, and manage accounts in Setup > Users",
    "KITCHEN_TOKEN": "role tokens were replaced by named users: sign in, and manage accounts in Setup > Users",
    "APPROVAL_TIMEOUT_S": "approval timeouts count service minutes now: use APPROVAL_TIMEOUT_SIM_MIN",
}


def obsolete_settings(env_file: "str | Path | None" = None) -> list[tuple[str, str]]:
    """[(key, what to do)] for removed settings found in the environment or the .env file."""
    keys = set(os.environ)
    path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.split("=", 1)[0].replace("export ", "").strip())
    return [(k, hint) for k, hint in OBSOLETE.items() if k in keys]


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
