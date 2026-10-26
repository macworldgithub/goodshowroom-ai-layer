"""Verified identity mappings and safe, non-executable app settings."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from goodshowroom_ai.storage.identities import Record, SourceReference
from goodshowroom_ai.storage.models import utc_datetime, validate_bson_document


class IdentityMapping(Record):
    user_id: UUID
    source_ref: SourceReference
    mapping_version: int = Field(default=1, ge=1)
    active: bool = False
    sites: tuple[str, ...] = Field(min_length=1, max_length=100)
    verified_by: UUID
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def verification_time(cls, value):
        return utc_datetime(value)

    @model_validator(mode="after")
    def verified_identity(self):
        if self.source_ref.collection not in {
            "users",
            "staff_site_memberships",
        } or self.source_ref.key_field not in {"_id", "id", "user_id", "staff_id"}:
            raise ValueError("Stable source-qualified staff identity required")
        if not self.created_at <= self.verified_at <= self.updated_at:
            raise ValueError("Invalid verification chronology")
        if len(set(self.sites)) != len(self.sites) or any(
            not value
            or len(value) > 100
            or not all(c.isalnum() or c in "_-" for c in value)
            for value in self.sites
        ):
            raise ValueError("Canonical site identifiers required")
        return self

    def document(self):
        result = super().document()
        result["verified_at"] = self.verified_at
        validate_bson_document(result)
        return result


class SiteConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    site_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    aliases: tuple[str, ...] = Field(default=(), max_length=100)
    timezone: str | None = Field(default=None, max_length=100)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Unknown timezone") from None
        return value

    @field_validator("aliases")
    @classmethod
    def bounded_aliases(cls, values):
        if any(
            not v.strip()
            or not v.isprintable()
            or any(c in v for c in "<>$*")
            or len(v) > 100
            or v.strip().casefold() in {"all sites", "*"}
            for v in values
        ) or len(set(v.casefold() for v in values)) != len(values):
            raise ValueError("Invalid site aliases")
        return values


class AppSetting(Record):
    id: Literal["sites", "retention", "disclosure"]
    sites: tuple[SiteConfiguration, ...] = Field(default=(), max_length=100)
    saved_retention_days: int | None = Field(default=None, ge=1, le=36500)
    audit_retention_days: int | None = Field(default=None, ge=1, le=36500)
    disclosure_enabled: Literal[False] = False

    @model_validator(mode="after")
    def typed_settings(self):
        if (
            self.id != "sites"
            and self.sites
            or self.id != "retention"
            and (
                self.saved_retention_days is not None
                or self.audit_retention_days is not None
            )
        ):
            raise ValueError("Settings do not match the document type")
        names = [s.site_id for s in self.sites]
        aliases = [
            a.strip().casefold() for s in self.sites for a in (s.site_id, *s.aliases)
        ]
        if len(set(names)) != len(names) or len(set(aliases)) != len(aliases):
            raise ValueError("Ambiguous site identifiers or aliases")
        return self
