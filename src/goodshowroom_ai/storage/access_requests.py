"""Access-request storage records. Approval workflows belong to services."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goodshowroom_ai.storage.identities import Grant, UserRecord
from goodshowroom_ai.storage.models import utc_datetime, validate_bson_document

AccessStatus = Literal["pending", "approved", "rejected", "consumed", "revoked"]


def scope_within(scope: Grant, allowed: Grant) -> bool:
    """Conservative narrowing only; never combine fields across separate grants."""
    if scope.dataset != allowed.dataset:
        return False
    if (
        allowed.record_mode != "all_permitted"
        and scope.record_mode != allowed.record_mode
    ):
        return False
    if allowed.record_mode == "selected" and any(
        item not in allowed.selected_records for item in scope.selected_records
    ):
        return False
    return all(
        set(getattr(scope, key)) <= set(getattr(allowed, key))
        for key in ("sites", "fields", "metrics", "tools")
    )


class AccessRequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: UUID
    schema_version: Literal[1] = 1
    kind: Literal["request", "preapproval"]
    email_key: str = Field(min_length=3, max_length=254)
    user_id: UUID | None = None
    status: AccessStatus = "pending"
    active: bool = True
    role_id: UUID | None = None
    scope_limit: tuple[Grant, ...] = Field(default=(), max_length=100)
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("email_key")
    @classmethod
    def email(cls, value):
        return UserRecord.email(value)

    @field_validator("created_at", "updated_at", "reviewed_at", "expires_at")
    @classmethod
    def dates(cls, value):
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def state(self):
        if self.updated_at < self.created_at:
            raise ValueError("Invalid record chronology")
        if self.active != (self.status == "pending"):
            raise ValueError("Only pending records can be active")
        if self.kind == "request" and self.user_id is None:
            raise ValueError("Requests require a user")
        if self.kind == "request" and self.status == "consumed":
            raise ValueError("Requests cannot be consumed")
        if self.kind == "preapproval" and (
            self.user_id is not None or self.role_id is None or not self.scope_limit
        ):
            raise ValueError(
                "Preapproval requires role and scope without a placeholder user"
            )
        if self.kind == "preapproval" and self.status in ("approved", "rejected"):
            raise ValueError("Invalid preapproval status")
        if self.status == "approved" and (self.role_id is None or not self.scope_limit):
            raise ValueError("Approval requires explicit role and scope")
        if (
            self.kind == "request"
            and self.status == "pending"
            and (self.role_id is not None or self.scope_limit)
        ):
            raise ValueError("Pending requests carry no grants")
        if (self.reviewed_by is None) != (self.reviewed_at is None):
            raise ValueError("Incomplete reviewer metadata")
        if self.status != "pending" and self.reviewed_by is None:
            raise ValueError("Closed records require reviewer metadata")
        if (
            self.reviewed_at is not None
            and not self.created_at <= self.reviewed_at <= self.updated_at
        ):
            raise ValueError("Invalid review chronology")
        return self

    def available(self, now: datetime) -> bool:
        now = utc_datetime(now)
        return self.active and (self.expires_at is None or now < self.expires_at)

    def document(self) -> dict:
        result = self.model_dump(mode="json")
        result["_id"] = result.pop("id")
        for key in ("created_at", "updated_at", "reviewed_at", "expires_at"):
            result[key] = getattr(self, key)
        validate_bson_document(result)
        return result
