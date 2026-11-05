"""Local configuration without business connections."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Only setup settings are consumed at this stage."""

    model_config = SettingsConfigDict(
        env_file=(
            Path(__file__).resolve().parents[2] / ".env",
            Path(__file__).resolve().parents[2] / ".env.local",
        ),
        extra="ignore",
        hide_input_in_errors=True,
    )
    app_env: Literal["development", "test", "production", "unknown"] = "unknown"
    openai_tracing_enabled: bool = False
    openai_tracing_include_sensitive_data: bool = False
    identity_enabled: bool = False
    identity_issuer: str = ""
    identity_audience: str = ""
    identity_key_id: str = ""
    identity_public_key: str = ""
    identity_previous_keys: str = ""
    identity_key_activated_at: int = 0
    identity_bootstrap_email: str = ""
    identity_bootstrap_role_id: str = ""
    identity_bootstrap_scope: str = ""
    app_mongo_uri: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_LAYER_MONGO_URI", "app_mongo_uri"),
    )
    cursor_encryption_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AI_LAYER_CURSOR_ENCRYPTION_KEY", "cursor_encryption_key"
        ),
    )

    @field_validator("identity_key_activated_at", mode="before")
    @classmethod
    def empty_identity_key_time_is_unconfigured(cls, value):
        return 0 if value == "" else value

    @field_validator("cursor_encryption_key", mode="before")
    @classmethod
    def empty_cursor_key_is_unconfigured(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("app_env", mode="before")
    @classmethod
    def unknown_environment_disables_tracing(cls, value):
        return value if value in ("development", "test", "production") else "unknown"

    @field_validator("openai_tracing_enabled", mode="before")
    @classmethod
    def explicit_tracing_enablement_only(cls, value):
        return value is True or (
            isinstance(value, str) and value.strip().lower() == "true"
        )

    @field_validator("openai_tracing_include_sensitive_data", mode="before")
    @classmethod
    def sensitive_tracing_is_never_permitted(cls, value):
        return False
