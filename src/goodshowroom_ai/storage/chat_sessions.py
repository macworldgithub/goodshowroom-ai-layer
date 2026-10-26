"""Bounded app-only records for persistent saved-chat parent sessions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goodshowroom_ai.execution import MAX_PAGE_SIZE
from goodshowroom_ai.storage.identities import Record
from goodshowroom_ai.storage.models import utc_datetime, validate_bson_document


class _ChatContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ChatQuestionState(_ChatContract):
    """Small, validated Q-01 continuation state; never stores user prose."""

    question_id: Literal["Q-01"] = "Q-01"
    metric_id: Literal["total_leads"] = "total_leads"
    metric_version: int = Field(ge=1)
    site: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    start_utc: datetime
    end_utc: datetime
    timezone: str = Field(min_length=1, max_length=100)
    policy_version: int = Field(ge=0)
    through_sequence: int = Field(ge=1)

    @field_validator("start_utc", "end_utc")
    @classmethod
    def dates(cls, value: datetime) -> datetime:
        return utc_datetime(value)

    @model_validator(mode="after")
    def ordered_period(self):
        if self.start_utc >= self.end_utc:
            raise ValueError("Question-state period is invalid")
        return self


class LastTurnAttempt(_ChatContract):
    """Minimal retry metadata; never stores message, result, or SDK items."""

    request_id: UUID
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_version: int = Field(ge=0)
    run_id: UUID
    process_id: UUID
    status: Literal["processing", "committed", "failed", "interrupted", "unknown"]
    started_at: datetime
    updated_at: datetime
    error_category: (
        Literal[
            "model_error",
            "validation_error",
            "timeout",
            "cancelled",
            "storage_error",
            "authorization_changed",
        ]
        | None
    ) = None

    @field_validator("started_at", "updated_at")
    @classmethod
    def dates(cls, value: datetime) -> datetime:
        return utc_datetime(value)

    @model_validator(mode="after")
    def status_metadata(self):
        if self.updated_at < self.started_at:
            raise ValueError("Attempt update precedes start")
        if (self.status in {"failed", "interrupted"}) != (
            self.error_category is not None
        ):
            raise ValueError("Attempt error metadata is invalid")
        if self.status in {"processing", "committed", "unknown"} and (
            self.error_category is not None
        ):
            raise ValueError("Attempt status cannot include an error category")
        return self


class ChatSessionRecord(Record):
    """Saved-chat metadata only; complete turns live in a separate collection."""

    owner_user_id: UUID
    creation_request_id: UUID
    title: Literal["New chat"] = "New chat"
    status: Literal["active", "deleting"] = "active"
    last_activity_at: datetime
    context_state: ChatQuestionState | None = None
    summary: None = None
    last_attempt: LastTurnAttempt | None = None
    expires_at: datetime | None = None

    @field_validator("last_activity_at", "expires_at")
    @classmethod
    def dates(cls, value: datetime | None) -> datetime | None:
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def chronology(self):
        if self.last_activity_at < self.created_at:
            raise ValueError("Chat activity precedes creation")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("Chat expiry must follow creation")
        return self

    def document(self) -> dict:
        value = super().document()
        value["last_activity_at"] = self.last_activity_at
        if self.context_state is not None:
            value["context_state"]["start_utc"] = self.context_state.start_utc
            value["context_state"]["end_utc"] = self.context_state.end_utc
        value["last_attempt"] = (
            self.last_attempt.model_dump(mode="json")
            if self.last_attempt is not None
            else None
        )
        if self.last_attempt is not None:
            value["last_attempt"]["started_at"] = self.last_attempt.started_at
            value["last_attempt"]["updated_at"] = self.last_attempt.updated_at
        value["expires_at"] = self.expires_at
        validate_bson_document(value)
        return value


class ChatSessionSummary(_ChatContract):
    """Safe metadata returned by the owner-scoped saved-chat list."""

    id: UUID
    title: Literal["New chat"]
    version: int = Field(ge=0)
    last_activity_at: datetime

    @field_validator("last_activity_at")
    @classmethod
    def date(cls, value: datetime) -> datetime:
        return utc_datetime(value)


class ChatSessionPosition(_ChatContract):
    """Internal continuation for one fixed owner-scoped chat-list order."""

    owner_user_id: UUID
    policy_version: int = Field(ge=0)
    page_size: int = Field(ge=1, le=MAX_PAGE_SIZE)
    last_activity_at: datetime
    session_id: UUID

    @field_validator("last_activity_at")
    @classmethod
    def date(cls, value: datetime) -> datetime:
        return utc_datetime(value)


class ChatReviewPosition(_ChatContract):
    """Caller- and target-bound continuation for audited admin chat review."""

    actor_user_id: UUID
    owner_user_id: UUID
    policy_version: int = Field(ge=0)
    page_size: int = Field(ge=1, le=MAX_PAGE_SIZE)
    last_activity_at: datetime
    session_id: UUID
    since: datetime | None = None
    until: datetime | None = None

    @field_validator("last_activity_at", "since", "until")
    @classmethod
    def dates(cls, value: datetime | None) -> datetime | None:
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def date_window(self):
        if (self.since is None) != (self.until is None):
            raise ValueError("Review date filters must be paired")
        if (
            self.since is not None
            and self.until is not None
            and self.since >= self.until
        ):
            raise ValueError("Review date window is invalid")
        return self


class ChatSessionPage(_ChatContract):
    """One bounded safe page and an opaque continuation token, if required."""

    chats: tuple[ChatSessionSummary, ...] = Field(max_length=MAX_PAGE_SIZE)
    next_cursor: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def bounded_page(self):
        if len(self.chats) > MAX_PAGE_SIZE:
            raise ValueError("Chat page exceeds the approved limit")
        return self
