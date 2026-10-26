"""Authenticated, encrypted cursors for fixed ascending operational pages."""

import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import model_validator

from goodshowroom_ai.api_contracts import ErrorCode
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.authorization import Contract
from goodshowroom_ai.execution import MAX_PAGE_SIZE
from goodshowroom_ai.operational_models import PagePosition
from goodshowroom_ai.storage.chat_sessions import (
    ChatReviewPosition,
    ChatSessionPosition,
)
from goodshowroom_ai.storage.chat_turns import ChatTurnPosition
from goodshowroom_ai.storage.models import utc_datetime

CURSOR_PURPOSE = "goodshowroom.operational-page.v1"
CURSOR_ORDER = "mongo_id_ascending"
CURSOR_TTL_SECONDS = 300
MAX_CURSOR_CHARS = 4_096
CHAT_CURSOR_PURPOSE = "goodshowroom.saved-chat-page.v1"
CHAT_CURSOR_ORDER = "last_activity_desc_id_desc"
CHAT_REVIEW_CURSOR_PURPOSE = "goodshowroom.saved-chat-review-page.v1"
CHAT_TURN_CURSOR_PURPOSE = "goodshowroom.saved-turn-page.v1"
CHAT_TURN_CURSOR_ORDER = "sequence_ascending"


class CursorRejected(ApiProblem):
    """Opaque cursor failure; input token and claims are never reflected."""

    def __init__(self, *, policy_changed: bool = False):
        super().__init__(
            ErrorCode.POLICY_CHANGED if policy_changed else ErrorCode.INVALID_REQUEST
        )


class CursorUnavailable(ApiProblem):
    """Cursor signing is unavailable because its purpose key is not configured."""

    def __init__(self):
        super().__init__(ErrorCode.AUTHORIZATION_UNAVAILABLE)


class _CursorPayload(Contract):
    schema_version: Literal[1] = 1
    purpose: Literal["goodshowroom.operational-page.v1"] = CURSOR_PURPOSE
    order: Literal["mongo_id_ascending"] = CURSOR_ORDER
    position: PagePosition

    @model_validator(mode="after")
    def stable_position(self):
        if self.position.after.key_field != "_id":
            raise ValueError("Stable cursor order is required")
        return self


class _ChatCursorPayload(Contract):
    schema_version: Literal[1] = 1
    purpose: Literal["goodshowroom.saved-chat-page.v1"] = CHAT_CURSOR_PURPOSE
    order: Literal["last_activity_desc_id_desc"] = CHAT_CURSOR_ORDER
    position: ChatSessionPosition


class _ChatReviewCursorPayload(Contract):
    schema_version: Literal[1] = 1
    purpose: Literal["goodshowroom.saved-chat-review-page.v1"] = (
        CHAT_REVIEW_CURSOR_PURPOSE
    )
    order: Literal["last_activity_desc_id_desc"] = CHAT_CURSOR_ORDER
    position: ChatReviewPosition


class _ChatTurnCursorPayload(Contract):
    schema_version: Literal[1] = 1
    purpose: Literal["goodshowroom.saved-turn-page.v1"] = CHAT_TURN_CURSOR_PURPOSE
    order: Literal["sequence_ascending"] = CHAT_TURN_CURSOR_ORDER
    position: ChatTurnPosition


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate cursor key")
        result[key] = value
    return result


class CursorCodec:
    """Encrypt internal positions and bind them to current caller/query policy."""

    def __init__(
        self,
        key: str | bytes,
        *,
        ttl_seconds: int = CURSOR_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ):
        try:
            if not isinstance(key, (str, bytes)) or type(ttl_seconds) is not int:
                raise ValueError()
            if not 1 <= ttl_seconds <= CURSOR_TTL_SECONDS:
                raise ValueError()
            key_bytes = key.encode("ascii") if isinstance(key, str) else key
            self._fernet = Fernet(key_bytes)
        except Exception:
            raise CursorUnavailable() from None
        self._ttl = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def encode(self, position: PagePosition) -> str:
        try:
            if not isinstance(position, PagePosition):
                raise ValueError()
            position = PagePosition.model_validate_json(
                position.model_dump_json(warnings="error")
            )
            if position.after.key_field != "_id":
                raise ValueError()
            payload = _CursorPayload(position=position)
            now = self._now()
            token = self._fernet.encrypt_at_time(
                payload.model_dump_json(warnings="error").encode("utf-8"),
                int(now.timestamp()),
            )
            result = token.decode("ascii")
            if len(result) > MAX_CURSOR_CHARS:
                raise ValueError()
            return result
        except CursorUnavailable:
            raise
        except Exception:
            raise CursorRejected() from None

    def decode(
        self,
        token: str,
        *,
        user_id: UUID,
        policy_version: int,
        request_digest: str,
    ) -> PagePosition:
        try:
            if (
                type(token) is not str
                or not 1 <= len(token) <= MAX_CURSOR_CHARS
                or type(user_id) is not UUID
                or type(policy_version) is not int
                or not 0 <= policy_version
                or type(request_digest) is not str
                or len(request_digest) != 64
                or any(char not in "0123456789abcdef" for char in request_digest)
            ):
                raise ValueError()

            raw_token = token.encode("ascii")
            timestamp = self._fernet.extract_timestamp(raw_token)
            now = self._now()
            now_seconds = int(now.timestamp())
            if timestamp > now_seconds or now_seconds - timestamp > self._ttl:
                raise InvalidToken()
            raw = self._fernet.decrypt_at_time(
                raw_token, ttl=self._ttl, current_time=now_seconds
            )
            decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            payload = _CursorPayload.model_validate_json(
                json.dumps(decoded, separators=(",", ":"))
            )
            position = payload.position
            if position.user_id != user_id:
                raise InvalidToken()
            if position.policy_version != policy_version:
                raise CursorRejected(policy_changed=True)
            if not hmac.compare_digest(position.request_digest, request_digest):
                raise InvalidToken()
            return position
        except CursorRejected:
            raise
        except CursorUnavailable:
            raise
        except Exception:
            raise CursorRejected() from None

    def encode_chat_page(self, position: ChatSessionPosition) -> str:
        """Seal the fixed saved-chat list position with the existing cursor key."""
        try:
            if not isinstance(position, ChatSessionPosition):
                raise ValueError()
            position = ChatSessionPosition.model_validate_json(
                position.model_dump_json(warnings="error")
            )
            payload = _ChatCursorPayload(position=position)
            now = self._now()
            token = self._fernet.encrypt_at_time(
                payload.model_dump_json(warnings="error").encode("utf-8"),
                int(now.timestamp()),
            )
            result = token.decode("ascii")
            if len(result) > MAX_CURSOR_CHARS:
                raise ValueError()
            return result
        except CursorUnavailable:
            raise
        except Exception:
            raise CursorRejected() from None

    def decode_chat_page(
        self,
        token: str,
        *,
        owner_user_id: UUID,
        policy_version: int,
        page_size: int,
    ) -> ChatSessionPosition:
        """Open a cursor only for its owner, current policy and fixed page size."""
        try:
            if (
                type(token) is not str
                or not 1 <= len(token) <= MAX_CURSOR_CHARS
                or type(owner_user_id) is not UUID
                or type(policy_version) is not int
                or not 0 <= policy_version
                or type(page_size) is not int
                or not 1 <= page_size <= MAX_PAGE_SIZE
            ):
                raise ValueError()
            raw_token = token.encode("ascii")
            timestamp = self._fernet.extract_timestamp(raw_token)
            now_seconds = int(self._now().timestamp())
            if timestamp > now_seconds or now_seconds - timestamp > self._ttl:
                raise InvalidToken()
            raw = self._fernet.decrypt_at_time(
                raw_token, ttl=self._ttl, current_time=now_seconds
            )
            decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            payload = _ChatCursorPayload.model_validate_json(
                json.dumps(decoded, separators=(",", ":"))
            )
            position = payload.position
            if (
                position.owner_user_id != owner_user_id
                or position.page_size != page_size
            ):
                raise InvalidToken()
            if position.policy_version != policy_version:
                raise CursorRejected(policy_changed=True)
            return position
        except CursorRejected:
            raise
        except CursorUnavailable:
            raise
        except Exception:
            raise CursorRejected() from None

    def encode_chat_review_page(self, position: ChatReviewPosition) -> str:
        """Seal an admin review position to both reviewer and target owner."""
        return self._encode_chat_position(position, _ChatReviewCursorPayload)

    def decode_chat_review_page(
        self,
        token: str,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        policy_version: int,
        page_size: int,
        since: datetime | None,
        until: datetime | None,
    ) -> ChatReviewPosition:
        position = self._decode_chat_position(token, _ChatReviewCursorPayload)
        if (
            position.actor_user_id != actor_user_id
            or position.owner_user_id != owner_user_id
            or position.policy_version != policy_version
            or position.page_size != page_size
            or position.since != since
            or position.until != until
        ):
            raise CursorRejected(
                policy_changed=position.policy_version != policy_version
            )
        return position

    def encode_chat_turn_page(self, position: ChatTurnPosition) -> str:
        """Seal a saved-turn position to one actor, chat, policy and snapshot."""
        return self._encode_chat_position(position, _ChatTurnCursorPayload)

    def decode_chat_turn_page(
        self,
        token: str,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        session_id: UUID,
        action: str,
        policy_version: int,
        page_size: int,
    ) -> ChatTurnPosition:
        position = self._decode_chat_position(token, _ChatTurnCursorPayload)
        if (
            position.actor_user_id != actor_user_id
            or position.owner_user_id != owner_user_id
            or position.session_id != session_id
            or position.action != action
            or position.policy_version != policy_version
            or position.page_size != page_size
        ):
            raise CursorRejected(
                policy_changed=position.policy_version != policy_version
            )
        return position

    def _encode_chat_position(self, position, payload_type) -> str:
        try:
            payload = payload_type(
                position=type(position).model_validate_json(
                    position.model_dump_json(warnings="error")
                )
            )
            now = self._now()
            token = self._fernet.encrypt_at_time(
                payload.model_dump_json(warnings="error").encode("utf-8"),
                int(now.timestamp()),
            )
            result = token.decode("ascii")
            if len(result) > MAX_CURSOR_CHARS:
                raise ValueError()
            return result
        except CursorUnavailable:
            raise
        except Exception:
            raise CursorRejected() from None

    def _decode_chat_position(self, token: str, payload_type):
        try:
            if type(token) is not str or not 1 <= len(token) <= MAX_CURSOR_CHARS:
                raise ValueError()
            raw_token = token.encode("ascii")
            timestamp = self._fernet.extract_timestamp(raw_token)
            now_seconds = int(self._now().timestamp())
            if timestamp > now_seconds or now_seconds - timestamp > self._ttl:
                raise InvalidToken()
            raw = self._fernet.decrypt_at_time(
                raw_token, ttl=self._ttl, current_time=now_seconds
            )
            decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            payload = payload_type.model_validate_json(
                json.dumps(decoded, separators=(",", ":"))
            )
            return payload.position
        except CursorRejected:
            raise
        except CursorUnavailable:
            raise
        except Exception:
            raise CursorRejected() from None

    def _now(self) -> datetime:
        try:
            current = utc_datetime(self._clock())
            if current.utcoffset() != UTC.utcoffset(current):
                raise ValueError()
            return current
        except Exception:
            raise CursorUnavailable() from None
