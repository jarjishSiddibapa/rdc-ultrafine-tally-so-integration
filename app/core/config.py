"""Application settings, loaded from environment / .env (never hardcode secrets)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Ultrafine Sales Orders"
    app_env: Literal["development", "production", "test"] = "development"
    host: str = "127.0.0.1"
    port: int = 1401
    secret_key: str = Field(min_length=32)
    allowed_hosts: str = "localhost,127.0.0.1"
    cookie_secure: bool = False

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "tally_so"
    test_db_name: str = "tally_so_test"

    admin_email: str = ""
    admin_password: str = ""
    admin_name: str = "Administrator"

    tally_api_url: str = ""
    tally_api_token: str = ""

    # Outgoing email. Mail is queued in the database and sent by a background worker, so a slow
    # or unreachable mail server never slows down or breaks a request.
    mail_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    mail_from: str = ""
    mail_from_name: str = "Ultrafine Sales Orders"
    app_base_url: str = "http://localhost:1401"   # used for links inside emails; never taken from the request
    reset_token_minutes: int = 30
    temp_password_hours: int = 72                 # an unused temporary (admin-issued) password stops working after this long
    pending_reminder_hours: int = 24              # remind approvers about orders waiting longer than this

    # Session / auth policy
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    max_body_bytes: int = 1_048_576  # 1 MiB; forms only, no uploads

    @field_validator("secret_key")
    @classmethod
    def _no_placeholder_secret(cls, v: str) -> str:
        if v.lower().startswith(("change", "secret", "dev")):
            raise ValueError("SECRET_KEY must be a random value")
        return v

    @field_validator("smtp_password", mode="before")
    @classmethod
    def _strip_app_password(cls, v: object) -> object:
        # Google shows app passwords as "abcd efgh ijkl mnop"; the real value has no spaces.
        return "".join(str(v).split()) if v is not None else v

    @field_validator("app_base_url")
    @classmethod
    def _clean_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("APP_BASE_URL must start with http:// or https://")
        return v

    @property
    def mail_configured(self) -> bool:
        return bool(self.mail_enabled and self.smtp_host and self.smtp_user and self.smtp_password.get_secret_value() and self.mail_from)

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.app_env == "production":
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production (serve over HTTPS)")
            if "*" in self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must not contain '*' in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    def database_url(self, database: str | None = None) -> URL:
        # URL.create escapes special characters in the password (e.g. '#', '@').
        return URL.create(
            "mysql+pymysql",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=database or self.db_name,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
