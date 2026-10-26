"""Public wire contracts, independent of storage and internal identity tokens.

Schemas validate data; callers still authorize ownership, data and every release.
Only the currently implemented response slice is exported. No endpoint is added.
"""

import json
import math
import re
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from goodshowroom_ai.authorization import Contract
from goodshowroom_ai.execution import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_MESSAGE_CHARACTERS,
    MAX_REQUEST_BYTES,
    MAX_WIRE_BYTES,
)
from goodshowroom_ai.responses import StructuredResponse

MAX_SAFE_INTEGER = 9_007_199_254_740_991
# Structural contract bounds; ingress/admission mechanics belong to TICKET-057.
_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


class InvalidContract(ValueError):
    """Fixed public category. Never attach validation inputs or raw JSON."""


class ErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    SESSION_EXPIRED = "session_expired"
    APPROVAL_REQUIRED = "approval_required"
    ACCESS_DENIED = "access_denied"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    REQUEST_IN_PROGRESS = "request_in_progress"
    POLICY_CHANGED = "policy_changed"
    TEMPORARY_UNAVAILABLE = "temporary_unavailable"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    MODEL_FAILURE = "model_failure"
    RESPONSE_INVALID = "response_invalid"
    SOURCE_UNAVAILABLE = "source_unavailable"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    PERSISTENCE_FAILED = "persistence_failed"
    CLEANUP_PENDING = "cleanup_pending"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL_ERROR = "internal_error"


# One fixed catalog feeds backend error mapping and generated frontend validation.
ERROR_CATALOG = {
    ErrorCode.AUTHENTICATION_REQUIRED: (401, "Please sign in.", False),
    ErrorCode.SESSION_EXPIRED: (
        401,
        "Your session has expired. Please sign in.",
        False,
    ),
    ErrorCode.APPROVAL_REQUIRED: (403, "Administrator approval is required.", False),
    ErrorCode.ACCESS_DENIED: (
        403,
        "You do not have access to this information.",
        False,
    ),
    ErrorCode.NOT_FOUND: (404, "The requested resource is unavailable.", False),
    ErrorCode.METHOD_NOT_ALLOWED: (405, "This request method is not allowed.", False),
    ErrorCode.VERSION_CONFLICT: (
        409,
        "The resource changed. Reload before continuing.",
        False,
    ),
    ErrorCode.IDEMPOTENCY_CONFLICT: (
        409,
        "This request ID was used for a different request.",
        False,
    ),
    ErrorCode.REQUEST_IN_PROGRESS: (409, "This request is still in progress.", True),
    ErrorCode.POLICY_CHANGED: (
        409,
        "Your permissions changed. Reload before continuing.",
        False,
    ),
    ErrorCode.TEMPORARY_UNAVAILABLE: (
        410,
        "This temporary chat is unavailable. Start a new chat.",
        False,
    ),
    ErrorCode.PAYLOAD_TOO_LARGE: (413, "The request is too large.", False),
    ErrorCode.INVALID_REQUEST: (422, "The request is invalid.", False),
    ErrorCode.RATE_LIMITED: (
        429,
        "Too many requests. Please wait before continuing.",
        True,
    ),
    ErrorCode.MODEL_FAILURE: (502, "The answer could not be prepared.", False),
    ErrorCode.RESPONSE_INVALID: (502, "The answer could not be validated.", False),
    ErrorCode.SOURCE_UNAVAILABLE: (503, "The data source is unavailable.", False),
    ErrorCode.AUTHORIZATION_UNAVAILABLE: (503, "Access could not be verified.", False),
    ErrorCode.PERSISTENCE_FAILED: (
        503,
        "The save could not be confirmed. Check the request status.",
        False,
    ),
    ErrorCode.CLEANUP_PENDING: (503, "Cleanup is not complete.", True),
    ErrorCode.DEADLINE_EXCEEDED: (
        504,
        "The request timed out. Check its status before retrying.",
        False,
    ),
    ErrorCode.INTERNAL_ERROR: (500, "The request could not be completed.", False),
}
COMMIT_ERROR_CODES = frozenset(
    {
        ErrorCode.PERSISTENCE_FAILED,
        ErrorCode.CLEANUP_PENDING,
        ErrorCode.POLICY_CHANGED,
        ErrorCode.DEADLINE_EXCEEDED,
        ErrorCode.INTERNAL_ERROR,
    }
)


def _canonical_uuid(value):
    if isinstance(value, str) and not _UUID.fullmatch(value):
        raise ValueError("Invalid UUID")
    return value


class CreateChatRequest(Contract):
    request_id: UUID

    _uuid = field_validator("request_id", mode="before")(_canonical_uuid)


class TurnRequest(CreateChatRequest):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)
    expected_version: int = Field(ge=0, le=MAX_SAFE_INTEGER)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class ChatTurnData(Contract):
    session_id: UUID
    mode: Literal["saved", "temporary"]
    version: int = Field(ge=1, le=MAX_SAFE_INTEGER)
    persistence: Literal["committed", "not_saved"]
    response: StructuredResponse

    _uuid = field_validator("session_id", mode="before")(_canonical_uuid)

    @model_validator(mode="after")
    def persistence_matches_mode(self):
        if self.persistence != ("committed" if self.mode == "saved" else "not_saved"):
            raise ValueError("Inconsistent persistence")
        return self


class ChatResultEnvelope(CreateChatRequest):
    schema_version: Literal[1] = 1
    data: ChatTurnData


class ApiError(Contract):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=200)
    retryable: bool
    commit_state: Literal["not_committed", "unknown"] | None = None

    @model_validator(mode="after")
    def fixed_catalog(self):
        _, message, retryable = ERROR_CATALOG[self.code]
        if self.message != message or self.retryable != retryable:
            raise ValueError("Error must use the safe catalog")
        if self.commit_state is not None and self.code not in COMMIT_ERROR_CODES:
            raise ValueError("Commit state is not valid for this error")
        return self


class ErrorEnvelope(CreateChatRequest):
    schema_version: Literal[1] = 1
    error: ApiError


PUBLIC_MODELS = (CreateChatRequest, TurnRequest, ChatResultEnvelope, ErrorEnvelope)


def _bounded_tree(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise ValueError("JSON structure exceeds contract bounds")
        if isinstance(current, dict):
            for key, value in current.items():
                if key in {"request_id", "session_id"} and (
                    not isinstance(value, str) or not _UUID.fullmatch(value)
                ):
                    raise ValueError("Invalid wire UUID")
                if (
                    key
                    in {
                        "start_local",
                        "end_local",
                        "start_utc",
                        "end_utc",
                        "retrieved_at",
                    }
                    and value is not None
                ):
                    if not isinstance(value, str) or not _DATETIME.fullmatch(value):
                        raise ValueError("Invalid wire timestamp")
                    if key not in {"start_local", "end_local"} and not value.endswith(
                        ("Z", "+00:00")
                    ):
                        raise ValueError("Wire timestamp must use UTC")
            stack.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            stack.extend((v, depth + 1) for v in current)
        elif type(current) in {int, float} and (
            type(current) is float
            or not math.isfinite(current)
            or abs(current) > MAX_SAFE_INTEGER
        ):
            raise ValueError("Number is outside the shared wire range")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Nonfinite JSON number")


def _explicit_wire_fields(expected, actual):
    if type(expected) is not type(actual):
        # Pydantic literal equality can otherwise accept true as the constant 1
        # or zero as false. Keep JSON types aligned with the frontend schema.
        raise ValueError("Wire types differ")
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key == "commit_state" and key not in actual:
                continue
            if key not in actual:
                raise ValueError("Wire field is missing")
            _explicit_wire_fields(value, actual[key])
    elif isinstance(expected, list):
        for value, supplied in zip(expected, actual, strict=True):
            _explicit_wire_fields(value, supplied)


def parse_contract(model, raw: bytes):
    """Validate bounded UTF-8 JSON without returning raw Pydantic error details.

    Only explicitly exported models qualify. This is not a replacement for the
    future pre-body ingress limits, authorization or admission controls.
    """
    try:
        if model not in PUBLIC_MODELS or not isinstance(raw, bytes):
            raise ValueError()
        maximum = (
            MAX_REQUEST_BYTES
            if model in {TurnRequest, CreateChatRequest}
            else MAX_WIRE_BYTES
        )
        if len(raw) > maximum:
            raise ValueError()
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        _bounded_tree(decoded)
        # Keep Pydantic JSON mode for strict UUID/datetime parsing.
        result = model.model_validate_json(raw)
        _explicit_wire_fields(result.model_dump(mode="json", warnings="error"), decoded)
        return result
    except Exception:
        raise InvalidContract("Request or response is invalid.") from None
