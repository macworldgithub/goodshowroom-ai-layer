"""Strict types shared by the app-only Mongo repositories."""

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Literal
from uuid import UUID, uuid4

from bson import BSON
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_APP_DOCUMENT_BYTES = 1_048_576


class StorageCapability(StrEnum):
    """Narrow internal capabilities for app storage writes."""

    ADVANCE_ACCESS_CONTROL = "advance_access_control"
    WRITE_REQUIRED_AUDIT = "write_required_audit"
    READ_IDENTITIES = "read_identities"
    WRITE_IDENTITIES = "write_identities"
    READ_ACCESS_REQUESTS = "read_access_requests"
    WRITE_ACCESS_REQUESTS = "write_access_requests"
    READ_LOGINS = "read_logins"
    WRITE_LOGINS = "write_logins"


class InternalAuthorizationContext(BaseModel):
    """Typed repository context; callers must supply explicit capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    actor_id: UUID | None = None
    capabilities: frozenset[StorageCapability] = Field(default_factory=frozenset)

    @field_validator("capabilities", mode="before")
    @classmethod
    def reject_untyped_capabilities(cls, value: Any) -> Any:
        if value is None:
            return frozenset()
        if not isinstance(value, (set, frozenset, tuple, list)):
            raise ValueError("Storage capabilities must be typed")
        if any(not isinstance(item, StorageCapability) for item in value):
            raise ValueError("Storage capabilities must be typed")
        return frozenset(value)

    def permits(self, capability: StorageCapability) -> bool:
        return self.actor_id is not None and capability in self.capabilities


AuditAction = Literal["access_control_change", "security_outcome", "admin_review"]
AuditOutcome = Literal["success", "denied", "failure"]
AuditErrorCategory = Literal[
    "authorization_denied", "version_conflict", "persistence_failure"
]


class AppAuditEvent(BaseModel):
    """Minimal audit metadata. Free text, transcripts, and raw errors are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(default=1, ge=1)
    actor_id: UUID
    target_id: UUID | None = None
    action: AuditAction
    outcome: AuditOutcome
    error_category: AuditErrorCategory | None = None
    request_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_date(cls, value: datetime) -> datetime:
        return utc_datetime(value)

    def to_bson_document(self) -> dict[str, Any]:
        document = self.model_dump(mode="python")
        document["_id"] = normalize_uuid(uuid4())
        document["actor_id"] = normalize_uuid(self.actor_id)
        document["target_id"] = (
            normalize_uuid(self.target_id) if self.target_id is not None else None
        )
        document["request_id"] = normalize_uuid(self.request_id)
        validate_bson_document(document)
        return document


def normalize_uuid(value: UUID | str) -> str:
    """Return a canonical UUID string and reject non-canonical string forms."""
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise ValueError("UUID must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("UUID must be a canonical UUID string") from None
    if str(parsed) != value:
        raise ValueError("UUID must be a canonical UUID string")
    return value


def utc_datetime(value: datetime) -> datetime:
    """Require an aware date and normalize it to UTC for BSON storage."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Date must include a UTC offset")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        raise ValueError("Date must include a valid UTC offset") from None
    if offset is None:
        raise ValueError("Date must include a UTC offset")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError("Date is outside the supported UTC range") from None


def validate_bson_document(
    document: Mapping[str, Any], *, max_bytes: int = MAX_APP_DOCUMENT_BYTES
) -> int:
    """Validate aware dates, BSON encodability, and the one MiB storage cap."""
    if not isinstance(document, Mapping):
        raise ValueError("App document must be a mapping")
    if (
        type(max_bytes) is not int
        or max_bytes < 1
        or max_bytes > MAX_APP_DOCUMENT_BYTES
    ):
        raise ValueError("App document size limit is invalid")
    _validate_values(document, active_containers=set(), depth=0)
    try:
        encoded = BSON.encode(dict(document))
    except Exception:
        raise ValueError("App document is not BSON encodable") from None
    size = len(encoded)
    if size > max_bytes:
        raise ValueError("App document exceeds the storage size limit")
    return size


def _validate_values(value: Any, *, active_containers: set[int], depth: int) -> None:
    if isinstance(value, datetime):
        utc_datetime(value)
        return
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("App document contains a non-finite number")
    if isinstance(value, Mapping):
        _validate_container(value, value.values(), active_containers, depth)
        return
    if isinstance(value, (list, tuple)):
        _validate_container(value, value, active_containers, depth)


def _validate_container(
    container: Any, values: Any, active_containers: set[int], depth: int
) -> None:
    if depth >= 100:
        raise ValueError("App document nesting exceeds the storage limit")
    identity = id(container)
    if identity in active_containers:
        raise ValueError("App document contains a circular value")
    active_containers.add(identity)
    try:
        for nested in values:
            _validate_values(
                nested, active_containers=active_containers, depth=depth + 1
            )
    finally:
        active_containers.remove(identity)
