"""Login metadata only: no tokens or conversation content."""

from datetime import datetime, timedelta
from uuid import UUID
from pydantic import Field, field_validator, model_validator
from goodshowroom_ai.storage.identities import Record
from goodshowroom_ai.storage.models import utc_datetime


class LoginRecord(Record):
    user_id: UUID
    generation: int = Field(default=0, ge=0)
    auth_started_at: datetime
    auth_expires_at: datetime
    expires_at: datetime
    lease_valid_until: datetime
    revoked_at: datetime | None = None
    purge_after: datetime | None = None

    @field_validator(
        "auth_expires_at",
        "auth_started_at",
        "expires_at",
        "lease_valid_until",
        "revoked_at",
        "purge_after",
    )
    @classmethod
    def dates_validity(cls, value):
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def bounds(self):
        retention_end = self.expires_at + timedelta(seconds=150)
        if self.purge_after is None:
            object.__setattr__(self, "purge_after", retention_end)
        elif self.purge_after < retention_end:
            raise ValueError("Replay binding retention is too short")
        if (
            not self.auth_started_at
            <= self.created_at
            < self.expires_at
            <= min(self.auth_expires_at, self.auth_started_at + timedelta(hours=8))
        ):
            raise ValueError("Invalid absolute login expiry")
        if (
            not self.created_at
            <= self.lease_valid_until
            <= min(self.expires_at, self.updated_at + timedelta(minutes=5))
        ):
            raise ValueError("Invalid login lease")
        if (
            self.revoked_at is not None
            and not self.created_at <= self.revoked_at <= self.updated_at
        ):
            raise ValueError("Invalid revocation time")
        return self

    def valid(self, now: datetime) -> bool:
        now = utc_datetime(now)
        return self.revoked_at is None and self.created_at <= now < min(
            self.expires_at, self.auth_expires_at, self.lease_valid_until
        )

    def document(self):
        result = super().document()
        for key in (
            "auth_expires_at",
            "auth_started_at",
            "expires_at",
            "lease_valid_until",
            "revoked_at",
            "purge_after",
        ):
            result[key] = getattr(self, key)
        return result
