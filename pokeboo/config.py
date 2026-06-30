"""Application configuration."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="POKEBOO_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Mode ---
    mode: Literal["online", "offline"] = "online"

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8000
    # Comma-separated list of allowed CORS origins for web deployment.
    # Desktop default is permissive ("*"). For production, set to your
    # Vercel URL, e.g. "https://pokeboo.vercel.app,https://www.pokeboo.vercel.app".
    allowed_origins: str = "*"
    # Secret key for signing session cookies (auth). Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    # MUST be set for web deployment (auth won't work without it).
    secret_key: str = Field(default="dev-secret-change-me", alias="POKEBOO_SECRET_KEY")
    # Frontend URL — used for OAuth callback redirects (where to send the user
    # after Google auth completes). Example: "https://pokeboo.vercel.app"
    # Desktop default is fine for local dev.
    frontend_url: str = Field(default="http://localhost:5173", alias="POKEBOO_FRONTEND_URL")
    # When False (desktop default), auth is OPTIONAL — the app works without
    # logging in. When True (web deployment), auth is REQUIRED — all endpoints
    # except /health, /keep-alive, /auth/* return 401 if not logged in.
    require_auth: bool = Field(default=False, alias="POKEBOO_REQUIRE_AUTH")

    # --- Storage ---
    # Desktop (default): SQLite at ~/.pokeboo/pokeboo.db
    # Web deployment: set POKEBOO_DB_URL to a PostgreSQL connection string
    # (e.g. postgresql://user:pass@host/dbname?sslmode=require). When set,
    # the Repository factory returns a PostgresRepository instead of SQLite.
    db_path: Path = Path("~/.pokeboo/pokeboo.db").expanduser()
    db_url: str = Field(default="", alias="POKEBOO_DB_URL")

    # --- Provider keys ---
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    # --- Model tier (distributed load based on complexity) ---
    # Default workhorse: Gemini 2.5 Flash — fast, cheap, supports thinking
    llm_model: str = "gemini-2.5-flash"
    # Premium tier for complex reasoning: Gemini 2.5 Pro
    llm_model_premium: str = "gemini-2.5-pro"
    # Embedding model: 1000 RPD — for semantic task search
    embedding_model: str = "gemini-embedding-001"
    # Lightweight text model (Gemma 3): for summaries, simple ops
    light_model: str = "gemma-3-27b-it"
    # TTS model: 10 RPD — for PokeBo's voice (when frontend ships)
    tts_model: str = "gemini-2.5-flash-preview-tts"
    # Live STT model: unlimited RPM — for real-time mic input
    live_stt_model: str = "gemini-2.5-flash-native-audio-dialog"

    # --- Thinking budget (for 2.5+ thinking models) ---
    llm_thinking_budget: int = 0

    # --- Model Router thresholds ---
    # Tasks with complexity >= this value get routed to the premium model
    router_complexity_threshold: int = 80
    # Tasks with complexity <= this value get routed to the light model (Gemma)
    router_light_threshold: int = 30

    # --- Scheduler tuning ---
    buffer_minutes: int = 5
    workday_start: str = "09:00"
    workday_end: str = "22:00"

    # --- Mood engine tuning ---
    mood_baseline: int = 75
    mood_min: int = 0
    mood_max: int = 100

    # --- PokeBo veto tuning ---
    relevancy_threshold: int = 65
    max_negotiation_rounds: int = 2

    # --- Google integration ---
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_token_path: Path = Path("~/.pokeboo/google_tokens.json").expanduser()
    calendar_sync_enabled: bool = False
    calendar_id: str = "primary"
    daily_brief_enabled: bool = False

    @field_validator("db_path", "google_token_path")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    def ensure_db_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.google_token_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
