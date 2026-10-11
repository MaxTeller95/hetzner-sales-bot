"""Static configuration loaded from environment variables / .env.

Values that the admin should be able to change at runtime (exchange rate, margin,
thresholds, texts...) live in the database instead; see app/services/runtime_settings.py.
The DEFAULT_* values below only seed those runtime settings.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # utf-8-sig tolerates a BOM, which some Windows editors add to .env files
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    # Telegram
    bot_token: str = ""
    admin_ids: str = ""  # comma-separated Telegram user ids that receive admin notifications
    redis_url: str = ""  # optional, for FSM storage; in-memory storage is used when empty

    # Database
    database_url: str = "sqlite+aiosqlite:///./hetzbot.db"

    # Used for session cookies and for encrypting secrets (root passwords, API tokens) at rest.
    # Changing it makes previously stored secrets unreadable.
    secret_key: str = "change-me"

    # Providers
    hetzner_api_token: str = ""

    # Admin dashboard
    admin_username: str = "admin"
    admin_password: str = ""
    web_base_url: str = "http://localhost:8000"
    session_https_only: bool = False

    # Abuse mailbox (optional). Hetzner sends abuse reports by e-mail.
    abuse_imap_host: str = ""
    abuse_imap_port: int = 993
    abuse_imap_user: str = ""
    abuse_imap_password: str = ""
    abuse_imap_folder: str = "INBOX"
    abuse_sender_filter: str = "hetzner"

    # Seeds for runtime settings
    default_exchange_rate_eur: int = 0
    default_exchange_rate_usd: int = 0
    default_profit_margin_percent: float = 35
    default_low_balance_threshold: int = 500_000

    worker_concurrency: int = 5
    log_level: str = "INFO"

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def check_production(self) -> None:
        """Refuse to run against a real database with an insecure secret."""
        if not self.is_sqlite and (self.secret_key == "change-me" or len(self.secret_key) < 32):
            raise SystemExit("SECRET_KEY must be set to a random string of at least 32 characters.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
