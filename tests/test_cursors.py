"""Offline tests for short-lived authenticated encrypted read cursors."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from goodshowroom_ai.api_contracts import ErrorCode
from goodshowroom_ai.cursors import CursorCodec, CursorRejected
from goodshowroom_ai.operational_models import PagePosition
from goodshowroom_ai.storage.identities import SourceReference
from goodshowroom_ai.storage.chat_sessions import ChatSessionPosition


@dataclass
class FakeClock:
    value: datetime = datetime(2026, 10, 26, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def cursor_case():
    clock = FakeClock()
    user_id = uuid4()
    digest = "a" * 64
    position = PagePosition(
        user_id=user_id,
        policy_version=4,
        request_digest=digest,
        after=SourceReference(
            database="byd-leads-new",
            collection="leads",
            key_field="_id",
            key_type="objectid",
            key_value="0123456789abcdef01234567",
        ),
    )
    codec = CursorCodec(Fernet.generate_key(), ttl_seconds=300, clock=clock)
    return codec, clock, position


def decode(codec, token, position):
    return codec.decode(
        token,
        user_id=position.user_id,
        policy_version=position.policy_version,
        request_digest=position.request_digest,
    )


def test_round_trip_keeps_position_but_hides_user_and_source_id(cursor_case):
    codec, _, position = cursor_case
    token = codec.encode(position)

    assert isinstance(token, str)
    assert token
    assert decode(codec, token, position) == position
    assert str(position.user_id) not in token
    assert position.after.key_value not in token
    assert position.request_digest not in token


def test_tampered_cursor_is_rejected_without_echoing_token(cursor_case):
    codec, _, position = cursor_case
    token = codec.encode(position)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(CursorRejected) as caught:
        decode(codec, tampered, position)

    assert token not in str(caught.value)
    assert position.after.key_value not in str(caught.value)


@pytest.mark.parametrize(
    "change",
    ["user_id", "policy_version", "request_digest"],
)
def test_cursor_cannot_be_reused_with_another_caller_or_filter_or_policy(
    cursor_case, change
):
    codec, _, position = cursor_case
    token = codec.encode(position)
    expected = {
        "user_id": position.user_id,
        "policy_version": position.policy_version,
        "request_digest": position.request_digest,
    }
    if change == "user_id":
        expected[change] = uuid4()
    elif change == "policy_version":
        expected[change] += 1
    else:
        expected[change] = "b" * 64

    with pytest.raises(CursorRejected) as caught:
        codec.decode(token, **expected)

    assert str(position.after.key_value) not in str(caught.value)


def test_cursor_expires_after_its_configured_lifetime(cursor_case):
    codec, clock, position = cursor_case
    token = codec.encode(position)
    clock.advance(300)
    assert decode(codec, token, position) == position
    clock.advance(1)

    with pytest.raises(CursorRejected):
        decode(codec, token, position)


def test_cursor_from_another_key_is_rejected(cursor_case):
    codec, clock, position = cursor_case
    token = codec.encode(position)
    other = CursorCodec(Fernet.generate_key(), ttl_seconds=300, clock=clock)

    with pytest.raises(CursorRejected):
        decode(other, token, position)


def test_position_must_use_the_fixed_stable_identifier_order(cursor_case):
    codec, _, position = cursor_case
    invalid = position.model_copy(
        update={"after": position.after.model_copy(update={"key_field": "customer_id"})}
    )

    with pytest.raises(Exception):
        codec.encode(invalid)


@pytest.fixture
def chat_cursor_case():
    clock = FakeClock()
    owner_id = uuid4()
    position = ChatSessionPosition(
        owner_user_id=owner_id,
        policy_version=8,
        page_size=25,
        last_activity_at=clock.value - timedelta(minutes=1),
        session_id=uuid4(),
    )
    codec = CursorCodec(Fernet.generate_key(), ttl_seconds=300, clock=clock)
    return codec, clock, position


def decode_chat(codec, token, position, **overrides):
    expected = {
        "owner_user_id": position.owner_user_id,
        "policy_version": position.policy_version,
        "page_size": position.page_size,
    }
    expected.update(overrides)
    return codec.decode_chat_page(token, **expected)


def test_saved_chat_cursor_round_trip_hides_owner_and_session(chat_cursor_case):
    codec, _, position = chat_cursor_case
    token = codec.encode_chat_page(position)

    assert decode_chat(codec, token, position) == position
    assert str(position.owner_user_id) not in token
    assert str(position.session_id) not in token


@pytest.mark.parametrize("claim", ["owner_user_id", "policy_version", "page_size"])
def test_saved_chat_cursor_is_bound_to_owner_policy_and_page_size(
    chat_cursor_case, claim
):
    codec, _, position = chat_cursor_case
    token = codec.encode_chat_page(position)
    supplied = {
        "owner_user_id": position.owner_user_id,
        "policy_version": position.policy_version,
        "page_size": position.page_size,
    }
    if claim == "owner_user_id":
        supplied[claim] = uuid4()
    elif claim == "policy_version":
        supplied[claim] += 1
    else:
        supplied[claim] += 1

    with pytest.raises(CursorRejected) as caught:
        codec.decode_chat_page(token, **supplied)

    assert str(position.session_id) not in str(caught.value)


def test_saved_chat_cursor_reports_stale_policy_without_revealing_claims(
    chat_cursor_case,
):
    codec, _, position = chat_cursor_case
    token = codec.encode_chat_page(position)

    with pytest.raises(CursorRejected) as caught:
        codec.decode_chat_page(
            token,
            owner_user_id=position.owner_user_id,
            policy_version=position.policy_version + 1,
            page_size=position.page_size,
        )

    assert caught.value.code is ErrorCode.POLICY_CHANGED
    assert str(position.session_id) not in str(caught.value)


def test_saved_chat_cursor_expires_and_rejects_tampering(chat_cursor_case):
    codec, clock, position = chat_cursor_case
    token = codec.encode_chat_page(position)
    clock.advance(301)

    with pytest.raises(CursorRejected):
        decode_chat(codec, token, position)

    clock.value -= timedelta(seconds=301)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(CursorRejected) as caught:
        decode_chat(codec, tampered, position)
    assert token not in str(caught.value)


def test_saved_chat_and_operational_cursor_purposes_are_not_interchangeable(
    cursor_case, chat_cursor_case
):
    op_codec, _, op_position = cursor_case
    chat_codec, _, chat_position = chat_cursor_case
    op_token = op_codec.encode(op_position)
    chat_token = chat_codec.encode_chat_page(chat_position)

    with pytest.raises(CursorRejected):
        decode_chat(chat_codec, op_token, chat_position)
    with pytest.raises(CursorRejected):
        decode(op_codec, chat_token, op_position)
