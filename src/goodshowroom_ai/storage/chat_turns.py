"""Typed, bounded records for authoritative saved-chat turns."""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goodshowroom_ai.execution import MAX_MESSAGE_CHARACTERS, MAX_PAGE_SIZE
from goodshowroom_ai.responses import BuiltResponse, StructuredResponse
from goodshowroom_ai.storage.chat_sessions import ChatQuestionState
from goodshowroom_ai.storage.models import (
    MAX_APP_DOCUMENT_BYTES,
    utc_datetime,
    validate_bson_document,
)


class _TurnContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TurnCommand(_TurnContract):
    """Client turn input after the API contract has validated it."""

    request_id: UUID
    expected_version: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)

    def request_hash(self, owner_id: UUID, session_id: UUID) -> str:
        if type(owner_id) is not UUID or type(session_id) is not UUID:
            raise ValueError("Turn ownership is invalid")
        canonical = json.dumps(
            [
                str(owner_id),
                str(session_id),
                str(self.request_id),
                self.expected_version,
                self.message,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class MetricVersion(_TurnContract):
    metric_id: Literal["total_leads"]
    version: int = Field(ge=1)


class ChatTurnRecord(_TurnContract):
    """One complete turn; intermediate model and tool items are excluded."""

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    owner_user_id: UUID
    request_id: UUID
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_version: int = Field(ge=0)
    sequence: int = Field(ge=1)
    user_message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)
    response: StructuredResponse
    question_state_after: ChatQuestionState | None = None
    policy_version: int = Field(ge=0)
    metric_versions: tuple[MetricVersion, ...] = Field(max_length=1)
    committed_at: datetime
    expires_at: datetime | None = None
    size_bytes: int = Field(ge=1, le=MAX_APP_DOCUMENT_BYTES)

    @field_validator("committed_at", "expires_at")
    @classmethod
    def dates(cls, value):
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def evidence_matches(self):
        if self.sequence != self.expected_version + 1:
            raise ValueError("Turn sequence does not match its parent version")
        if self.response.policy_version != self.policy_version:
            raise ValueError("Turn policy version does not match its response")
        expected = (
            (
                MetricVersion(
                    metric_id="total_leads",
                    version=self.response.evidence[0].metric_version,
                ),
            )
            if self.response.status == "complete"
            else ()
        )
        if self.metric_versions != expected:
            raise ValueError("Turn metric versions do not match response evidence")
        if self.question_state_after is not None:
            state = self.question_state_after
            if (
                state.policy_version != self.policy_version
                or state.through_sequence > self.sequence
            ):
                raise ValueError("Turn follow-up state is not grounded in this turn")
            if self.response.status == "complete" and (
                state.through_sequence != self.sequence
                or not self.metric_versions
                or state.metric_version != self.metric_versions[0].version
            ):
                raise ValueError("Turn follow-up state is not grounded in this turn")
            if (
                self.response.status != "complete"
                and state.through_sequence >= self.sequence
            ):
                raise ValueError("A notice cannot advance follow-up state")
        return self

    @classmethod
    def completed(
        cls,
        *,
        session_id: UUID,
        owner_user_id: UUID,
        command: TurnCommand,
        built_response: BuiltResponse,
        sequence: int,
        committed_at: datetime,
        expires_at: datetime | None,
        previous_state: ChatQuestionState | None,
    ) -> "ChatTurnRecord":
        if not isinstance(command, TurnCommand) or not isinstance(
            built_response, BuiltResponse
        ):
            raise ValueError("Validated turn input and response are required")
        response = built_response.response
        if built_response.request_id != command.request_id:
            raise ValueError("Built response request does not match the turn")
        state = None
        versions: tuple[MetricVersion, ...] = ()
        if response.status == "complete":
            fact = response.facts[0]
            period = response.period
            scope = response.scope
            if period is None or scope is None:
                raise ValueError("Complete response has no validated period or scope")
            versions = (
                MetricVersion(metric_id=fact.metric_id, version=fact.metric_version),
            )
            state = ChatQuestionState(
                question_id="Q-01",
                metric_id="total_leads",
                metric_version=fact.metric_version,
                site=scope.site,
                start_utc=period.start_utc,
                end_utc=period.end_utc,
                timezone=period.timezone,
                policy_version=response.policy_version,
                through_sequence=sequence,
            )
        elif (
            previous_state is not None
            and previous_state.policy_version == response.policy_version
        ):
            # A safe notice does not change previously validated follow-up state.
            state = previous_state
        record = cls(
            session_id=session_id,
            owner_user_id=owner_user_id,
            request_id=command.request_id,
            request_hash=command.request_hash(owner_user_id, session_id),
            expected_version=command.expected_version,
            sequence=sequence,
            user_message=command.message,
            response=response,
            question_state_after=state,
            policy_version=response.policy_version,
            metric_versions=versions,
            committed_at=committed_at,
            expires_at=expires_at,
            size_bytes=1,
        )
        for _ in range(4):
            size = validate_bson_document(
                record.document(), max_bytes=MAX_APP_DOCUMENT_BYTES
            )
            if size == record.size_bytes:
                return record
            record = record.model_copy(update={"size_bytes": size})
        raise ValueError("Turn size could not be stabilized")

    def document(self) -> dict:
        document = {
            "_id": str(self.id),
            "session_id": str(self.session_id),
            "owner_user_id": str(self.owner_user_id),
            "request_id": str(self.request_id),
            "request_hash": self.request_hash,
            "expected_version": self.expected_version,
            "sequence": self.sequence,
            "user_message": self.user_message,
            "response": self.response.model_dump(mode="python"),
            "question_state_after": self._question_state_document(),
            "policy_version": self.policy_version,
            "metric_versions": [
                item.model_dump(mode="python") for item in self.metric_versions
            ],
            "committed_at": self.committed_at,
            "expires_at": self.expires_at,
            "size_bytes": self.size_bytes,
        }
        validate_bson_document(document)
        return document

    def _question_state_document(self):
        if self.question_state_after is None:
            return None
        state = self.question_state_after.model_dump(mode="python")
        state["start_utc"] = self.question_state_after.start_utc
        state["end_utc"] = self.question_state_after.end_utc
        return state


class ChatTurnPosition(_TurnContract):
    """Opaque continuation bound to one actor, owner, chat and fixed snapshot."""

    actor_user_id: UUID
    owner_user_id: UUID
    session_id: UUID
    action: Literal["history", "review_saved_chats"]
    policy_version: int = Field(ge=0)
    page_size: int = Field(ge=1, le=MAX_PAGE_SIZE)
    snapshot_version: int = Field(ge=0)
    last_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def stable_snapshot(self):
        if self.last_sequence > self.snapshot_version:
            raise ValueError("Saved-turn cursor exceeds its snapshot")
        if self.action == "history" and self.actor_user_id != self.owner_user_id:
            raise ValueError("Owner history cursor identity is invalid")
        return self


class SavedTurnView(_TurnContract):
    """Validated saved content safe for an authorized history response."""

    id: UUID
    sequence: int = Field(ge=1)
    user_message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)
    response: StructuredResponse
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def date(cls, value: datetime) -> datetime:
        return utc_datetime(value)


class ChatTurnPage(_TurnContract):
    """One bounded saved-turn page after current-grant filtering."""

    turns: tuple[SavedTurnView, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=4_096)
    notice: (
        Literal[
            "Some saved messages are hidden because current access does not allow them."
        ]
        | None
    ) = None


class TurnAttemptResult(_TurnContract):
    status: Literal[
        "started", "committed", "processing", "failed", "interrupted", "unknown"
    ]
    run_id: UUID | None = None
    turn: ChatTurnRecord | None = None

    @model_validator(mode="after")
    def state_matches(self):
        if (self.status == "committed") != (self.turn is not None):
            raise ValueError("Attempt result turn is invalid")
        if (self.status in {"started", "processing"}) != (self.run_id is not None):
            raise ValueError("Attempt result run ID is invalid")
        return self


class TurnCommitResult(_TurnContract):
    status: Literal["committed", "unknown"]
    turn: ChatTurnRecord | None = None
    replayed: bool = False

    @model_validator(mode="after")
    def state_matches(self):
        if (self.status == "committed") != (self.turn is not None):
            raise ValueError("Commit result state is invalid")
        if self.status == "unknown" and self.replayed:
            raise ValueError("Unknown commit cannot be marked as replayed")
        return self


class TurnRecoveryResult(_TurnContract):
    status: Literal["committed", "processing", "retry_allowed", "unknown"]
    turn: ChatTurnRecord | None = None

    @model_validator(mode="after")
    def state_matches(self):
        if (self.status == "committed") != (self.turn is not None):
            raise ValueError("Recovery result state is invalid")
        return self
