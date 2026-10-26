"""Private user and role records; no login or grant-management workflows."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goodshowroom_ai.storage.models import utc_datetime, validate_bson_document


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: UUID
    schema_version: Literal[1] = 1
    version: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def dates(cls, value: datetime) -> datetime:
        return utc_datetime(value)

    def document(self) -> dict:
        value = self.model_dump(mode="json")
        value["_id"] = value.pop("id")
        value["created_at"] = self.created_at
        value["updated_at"] = self.updated_at
        validate_bson_document(value)
        return value

    @model_validator(mode="after")
    def chronology(self):
        if self.updated_at < self.created_at:
            raise ValueError("Updated date precedes creation")
        return self


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    database: Literal["byd-leads-new", "byd-panel"]
    collection: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    key_field: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$"
    )
    key_type: Literal["string", "objectid"]
    key_value: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def object_id(self):
        if self.key_type == "objectid" and (
            len(self.key_value) != 24
            or any(c not in "0123456789abcdef" for c in self.key_value)
        ):
            raise ValueError("Invalid canonical ObjectId")
        return self


class Grant(BaseModel):
    """Explicit bounded selectors; an empty scope grants nothing."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    dataset: Literal["byd-leads-new", "byd-panel"]
    sites: tuple[str, ...] = Field(min_length=1, max_length=100)
    record_mode: Literal["assigned", "selected", "all_permitted"]
    selected_records: tuple[SourceReference, ...] = Field(default=(), max_length=100)
    fields: tuple[str, ...] = Field(min_length=1, max_length=100)
    metrics: tuple[str, ...] = Field(default=(), max_length=100)
    tools: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("sites", "fields", "metrics", "tools")
    @classmethod
    def selectors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if (
                not value
                or len(value) > 100
                or not all(c.isalnum() or c in "_-" for c in value)
            ):
                raise ValueError("Invalid canonical selector")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate selectors")
        return values

    @model_validator(mode="after")
    def selected(self):
        if (self.record_mode == "selected") != bool(self.selected_records):
            raise ValueError("Selected records require explicit identifiers")
        if any(item.database != self.dataset for item in self.selected_records):
            raise ValueError("Selected records must match the dataset")
        return self


class UserRecord(Record):
    provider: Literal["google"] = "google"
    provider_account_id: str = Field(min_length=1, max_length=255)
    email_key: str = Field(min_length=3, max_length=254)
    display_name: str | None = Field(default=None, max_length=200)
    approval_status: Literal["pending", "approved", "rejected", "revoked"] = "pending"
    role_id: UUID | None = None
    scope_limit: tuple[Grant, ...] = Field(default=(), max_length=100)

    @field_validator("email_key")
    @classmethod
    def email(cls, value: str) -> str:
        value = value.strip().lower()
        if (
            value.count("@") != 1
            or not all(value.split("@"))
            or any(c.isspace() for c in value)
        ):
            raise ValueError("Invalid email key")
        return value

    @model_validator(mode="after")
    def approved_scope(self):
        if self.approval_status == "approved" and (
            self.role_id is None or not self.scope_limit
        ):
            raise ValueError("Approved users require role and explicit scope")
        return self


RoleCapability = Literal[
    "chat", "review_saved_chats", "manage_access", "manage_settings"
]


class RoleRecord(Record):
    name_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    active: bool = False
    grants: tuple[Grant, ...] = Field(default=(), max_length=100)
    capabilities: frozenset[RoleCapability] = Field(default_factory=frozenset)
