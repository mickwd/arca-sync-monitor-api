"""Configuración de la aplicación basada en variables de entorno (pydantic-settings)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Todas las opciones se pueden sobreescribir con variables de entorno o un archivo .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    app_name: str = "arca-sync-monitor-api"
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Base de datos ---
    database_url: str = "sqlite:///./arca_sync.db"

    # --- Seguridad / JWT ---
    jwt_secret: str = Field(default="change-me-in-production-please-32b", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "arca-sync-monitor-api"
    access_token_expire_minutes: int = 30
    password_hash_iterations: int = 210_000

    # Usuario administrador que se crea al iniciar si no existe
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # Cliente máquina-a-máquina (grant_type=client_credentials)
    oauth_client_id: str = "integration-client"
    oauth_client_secret: str = "integration-secret"
    oauth_client_scopes: str = "sync:write comprobantes:read monitoring:read"

    # --- ARCA ---
    arca_base_url: str = "http://127.0.0.1:5001"
    arca_cuit: str = "20123456789"
    arca_auth_mode: str = "token"  # "static" | "token"
    arca_static_token: str = "dev-token"
    arca_timeout_seconds: float = 15.0
    arca_page_size: int = 100
    sync_initial_date: date = date(2026, 1, 1)

    # --- Receptor ---
    receptor_base_url: str = "http://127.0.0.1:5001/receptor"
    receptor_api_key: str = "receptor-dev-key"
    receptor_timeout_seconds: float = 10.0

    # --- Webhooks entrantes del receptor ---
    webhook_secret: str = "webhook-dev-secret"
    webhook_tolerance_seconds: int = 300

    # --- Reintentos ---
    retry_max_attempts: int = 5
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 30.0

    # --- Scheduler ---
    scheduler_enabled: bool = False
    sync_interval_minutes: int = 15
    monitoring_interval_seconds: int = 60

    # --- Monitoreo ---
    hosts_config_path: Path = Path("config/hosts.yaml")
    cpu_warning: float = 80.0
    cpu_critical: float = 95.0
    memory_warning: float = 80.0
    memory_critical: float = 95.0
    disk_warning: float = 80.0
    disk_critical: float = 90.0
    alert_cooldown_minutes: int = 15

    # --- Notificaciones de alertas ---
    alert_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    alert_email_from: str = "monitor@example.com"
    alert_email_to: str | None = None

    @property
    def client_scopes(self) -> list[str]:
        return self.oauth_client_scopes.split()

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
