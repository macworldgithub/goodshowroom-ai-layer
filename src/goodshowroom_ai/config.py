"""Local configuration without business connections."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Only setup settings are consumed at this stage."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    app_env: Literal["development", "test", "production"] = "development"
    app_mongo_uri: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_LAYER_MONGO_URI", "app_mongo_uri"),
    )
