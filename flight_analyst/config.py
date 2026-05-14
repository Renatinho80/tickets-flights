"""
flight_analyst/config.py
Configuração centralizada da aplicação via Pydantic BaseSettings.
Lê variáveis de ambiente do .env ou do ambiente do sistema (Railway/Render).
"""

from enum import Enum
from pathlib import Path

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Any


class AppEnv(str, Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AmadeusEnv(str, Enum):
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Aplicação ---
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO
    api_port: int = 8000
    routes_config_path: Path = Path("routes.yaml")
    sqlite_path: Path = Path("data/flight_analyst.db")
    app_api_key: str = Field(default="dev_secret_key")
    dashboard_password: str = Field(default="dev_password")

    # --- Supabase ---
    supabase_url: str = Field(default="")
    supabase_key: str = Field(default="")

    # --- SerpApi ---
    serpapi_key: str = Field(default="")

    # --- Amadeus ---
    amadeus_client_id: str = Field(default="")
    amadeus_client_secret: str = Field(default="")
    amadeus_env: AmadeusEnv = AmadeusEnv.TEST

    # --- Google Gemini ---
    google_api_key: str = Field(default="")

    # --- Telegram ---
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # --- Inngest ---
    inngest_event_key: str = Field(default="")
    inngest_signing_key: str = Field(default="")

    # --- Sentry ---
    sentry_dsn: str = Field(default="")

    # --- Ntfy ---
    ntfy_topic: str = "flight-analyst-alerts"

    @field_validator("supabase_url", "supabase_key", mode="before")
    @classmethod
    def warn_if_empty(cls, v: str) -> str:
        return v or ""

    @field_validator("app_api_key", mode="after")
    @classmethod
    def validate_api_key_in_production(cls, v: str, info: Any) -> str:
        app_env = info.data.get("app_env")
        if app_env == AppEnv.PRODUCTION and v == "dev_secret_key":
            raise ValueError(
                "APP_API_KEY não pode ser o valor padrão 'dev_secret_key' em produção. "
                "Defina uma chave segura no .env ou nas variáveis de ambiente do Render."
            )
        return v

    @field_validator("dashboard_password", mode="after")
    @classmethod
    def validate_dashboard_password_in_production(cls, v: str, info: Any) -> str:
        app_env = info.data.get("app_env")
        if app_env == AppEnv.PRODUCTION and v == "dev_password":
            raise ValueError(
                "DASHBOARD_PASSWORD não pode ser o valor padrão 'dev_password' em produção. "
                "Defina uma senha segura no .env ou nas variáveis de ambiente."
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == AppEnv.PRODUCTION

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def has_serpapi(self) -> bool:
        return bool(self.serpapi_key)

    @property
    def has_amadeus(self) -> bool:
        return bool(self.amadeus_client_id and self.amadeus_client_secret)

    @property
    def has_gemini(self) -> bool:
        return bool(self.google_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def amadeus_base_url(self) -> str:
        if self.amadeus_env == AmadeusEnv.PRODUCTION:
            return "https://api.amadeus.com"
        return "https://test.api.amadeus.com"


# Instância singleton — importar de qualquer módulo
settings = Settings()
