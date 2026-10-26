"""Offline contracts for owner-bound saved-chat parent storage."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pymongo.errors import DuplicateKeyError

from goodshowroom_ai.authorization import (
    AuthorizationDecision,
    PolicySnapshot,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.cursors import CursorCodec, CursorRejected, CursorUnavailable
from goodshowroom_ai.storage.access_requests import Grant
from goodshowroom_ai.storage.chat_repository import ChatRepository
from goodshowroom_ai.storage.chat_sessions import ChatSessionPosition, ChatSessionRecord
from goodshowroom_ai.storage.identities import RoleRecord, UserRecord
from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppWriteConflict,
    AuthorizationDenied,
)
from tests.test_identity_storage import FakeClient, FakeCollection


NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
POLICY_VERSION = 7


class ChatCollection(FakeCollection):
    def __init__(self, name):
        super().__init__(name)
        self.queries = []
        self.cursors = []
        self.race_winner = None

    async def insert_one(self, document, *, session):
        if self.race_winner is not None:
            winner = self.race_winner
            self.race_winner = None
            self.documents[str(winner.id)] = winner.document()
            raise DuplicateKeyError("synthetic same-owner request race")
        return await super().insert_one(document, session=session)

    async def find_one(self, query, *, session):
        assert session is not None
        for document in self.documents.values():
            if all(document.get(key) == value for key, value in query.items()):
                return deepcopy(document)
        return None

    def find(self, query, *, projection, collation, max_time_ms, session):
        assert session is not None
        assert projection["_id"] == 1
        assert collation.document == {"locale": "simple"}
        assert max_time_ms == 5_000
        self.queries.append(deepcopy(query))
        documents = []
        owner = query["owner_user_id"]
        for document in self.documents.values():
            if document.get("owner_user_id") != owner:
                continue
            if document.get("status") != "active":
                continue
            expiry = document.get("expires_at")
            expiry_clause = query["$and"][0]["$or"]
            now = expiry_clause[2]["expires_at"]["$gt"]
            if expiry is not None and expiry <= now:
                continue
            if len(query["$and"]) > 1:
                clauses = query["$and"][1]["$or"]
                first = clauses[0]["last_activity_at"]["$lt"]
                same_activity = clauses[1]["last_activity_at"]
                before_id = clauses[1]["_id"]["$lt"]
                activity = document["last_activity_at"]
                if not (
                    activity < first
                    or (activity == same_activity and document["_id"] < before_id)
                ):
                    continue
            documents.append(deepcopy(document))
        documents.sort(
            key=lambda document: (document["last_activity_at"], document["_id"]),
            reverse=True,
        )
        cursor = FakeFindCursor(documents)
        self.cursors.append(cursor)
        return cursor


class FakeFindCursor:
    def __init__(self, documents):
        self.documents = documents
        self.closed = 0
        self.page_limit = None
        self.sort_fields = None

    def sort(self, fields):
        self.sort_fields = fields
        return self

    def limit(self, value):
        self.page_limit = value
        return self

    def batch_size(self, value):
        assert value == 1
        return self

    async def to_list(self, *, length):
        assert self.page_limit == length
        return self.documents[:length]

    async def close(self):
        self.closed += 1


class SavedChatFixture:
    def __init__(
        self, *, owner_id=None, active=True, approved=True, version=POLICY_VERSION
    ):
        self.owner_id = owner_id or uuid4()
        self.role_id = uuid4()
        self.login_id = uuid4()
        grant = Grant(
            dataset="byd-panel",
            sites=("site_a",),
            record_mode="all_permitted",
            fields=("customer_name",),
        )
        self.user = UserRecord(
            id=self.owner_id,
            provider_account_id=f"google-{self.owner_id}",
            email_key=f"{self.owner_id}@example.test",
            approval_status="approved" if approved else "pending",
            role_id=self.role_id,
            scope_limit=(grant,),
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
        )
        self.role = RoleRecord(
            id=self.role_id,
            name_key="chat_user",
            active=active,
            grants=(grant,),
            capabilities=frozenset({"chat"}),
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
        )
        self.principal = Principal(
            user_id=self.owner_id,
            login_id=self.login_id,
            generation=1,
        )
        self.login = LoginRecord(
            id=self.login_id,
            user_id=self.owner_id,
            generation=1,
            auth_started_at=NOW - timedelta(hours=2),
            auth_expires_at=NOW + timedelta(hours=6),
            expires_at=NOW + timedelta(hours=5),
            lease_valid_until=NOW + timedelta(minutes=5),
            created_at=NOW - timedelta(hours=2),
            updated_at=NOW,
        )
        self.snapshot = PolicySnapshot(
            policy_version=version,
            user=self.user,
            role=self.role,
            login=self.login,
        )
        self.client = FakeClient([])
        database = self.client.database
        database.collections["users"] = FakeCollection(
            "users", {str(self.user.id): self.user.document()}
        )
        database.collections["roles"] = FakeCollection(
            "roles", {str(self.role.id): self.role.document()}
        )
        database.collections["chat_sessions"] = ChatCollection("chat_sessions")
        database.collections["app_settings"].documents["access_control"][
            "policy_version"
        ] = version
        self.app_database = AppDatabase(self.client, database)
        self.codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)
        self.repository = ChatRepository(
            self.app_database,
            self.codec,
            clock=lambda: NOW,
        )

    def decision(self, *, action="chat", mode="saved"):
        return AuthorizationDecision(
            principal=self.principal,
            request=ProtectedAction(
                action=action,
                owner_id=self.owner_id,
                mode=mode,
            ),
            policy_version=self.snapshot.policy_version,
            snapshot=self.snapshot,
            constraints=(),
        )

    @property
    def chats(self):
        return self.client.database["chat_sessions"]


def make_chat(
    owner_id, request_id, *, created_at=NOW, status="active", expires_at=None
):
    return ChatSessionRecord(
        id=uuid4(),
        owner_user_id=owner_id,
        creation_request_id=request_id,
        title="New chat",
        status=status,
        version=0,
        created_at=created_at,
        updated_at=created_at,
        last_activity_at=created_at,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_create_replay_is_owner_bound_version_zero_and_has_no_turns():
    fixture = SavedChatFixture()
    request_id = uuid4()

    created = await fixture.repository.create_saved_chat(fixture.decision(), request_id)
    replay = await fixture.repository.create_saved_chat(fixture.decision(), request_id)

    assert created.id == replay.id
    assert created.owner_user_id == fixture.owner_id
    assert created.creation_request_id == request_id
    assert created.version == replay.version == 0
    assert len(fixture.chats.documents) == 1
    assert "chat_turns" not in fixture.client.database.collections


@pytest.mark.asyncio
async def test_concurrent_same_owner_create_recovers_with_one_read_only_retry():
    fixture = SavedChatFixture()
    request_id = uuid4()
    winner = make_chat(fixture.owner_id, request_id)
    fixture.chats.race_winner = winner

    recovered = await fixture.repository.create_saved_chat(
        fixture.decision(), request_id
    )

    assert recovered.id == winner.id
    assert recovered.creation_request_id == request_id
    assert len(fixture.chats.documents) == 1
    assert fixture.client.events.count("transaction-start") == 2


@pytest.mark.asyncio
async def test_repository_rejects_wrong_action_owner_or_mode_decision():
    fixture = SavedChatFixture()
    wrong_action = fixture.decision(action="review_saved_chats")
    wrong_owner = fixture.decision().model_copy(
        update={
            "request": fixture.decision().request.model_copy(
                update={"owner_id": uuid4()}
            )
        }
    )
    wrong_mode = fixture.decision().model_copy(
        update={
            "request": fixture.decision().request.model_copy(
                update={"mode": "temporary"}
            )
        }
    )

    for decision in (wrong_action, wrong_owner, wrong_mode):
        with pytest.raises(AuthorizationDenied):
            await fixture.repository.create_saved_chat(decision, uuid4())

    assert fixture.chats.documents == {}


@pytest.mark.asyncio
async def test_same_request_uuid_for_another_owner_does_not_reuse_or_reveal_chat():
    fixture = SavedChatFixture()
    request_id = uuid4()
    first = await fixture.repository.create_saved_chat(fixture.decision(), request_id)

    other = SavedChatFixture(owner_id=uuid4())
    fixture.client.database["users"].documents[str(other.user.id)] = (
        other.user.document()
    )
    fixture.client.database["roles"].documents[str(other.role.id)] = (
        other.role.document()
    )
    other.client.database = fixture.client.database
    other.app_database = AppDatabase(other.client, fixture.client.database)
    other.repository = ChatRepository(
        other.app_database, other.codec, clock=lambda: NOW
    )
    second = await other.repository.create_saved_chat(other.decision(), request_id)

    assert first.id != second.id
    assert first.owner_user_id != second.owner_user_id
    assert len(fixture.chats.documents) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expires_at",
    [
        ("deleting", None),
        ("active", NOW - timedelta(seconds=1)),
    ],
)
async def test_deleting_or_expired_parent_cannot_be_recovered(status, expires_at):
    fixture = SavedChatFixture()
    request_id = uuid4()
    record = make_chat(
        fixture.owner_id,
        request_id,
        status=status,
        created_at=NOW - timedelta(hours=1) if expires_at is not None else NOW,
        expires_at=expires_at,
    )
    fixture.chats.documents[str(record.id)] = record.document()

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.create_saved_chat(fixture.decision(), request_id)

    assert len(fixture.chats.documents) == 1


@pytest.mark.asyncio
async def test_temporary_mode_and_invalid_page_size_fail_without_storage_writes():
    fixture = SavedChatFixture()

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.create_saved_chat(
            fixture.decision(), uuid4(), mode="temporary"
        )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.list_saved_chats(
            fixture.decision(action="history"), page_size=101
        )

    assert fixture.chats.documents == {}


@pytest.mark.asyncio
async def test_repository_requires_approved_active_chat_role_and_current_policy():
    pending = SavedChatFixture(approved=False)
    with pytest.raises(AuthorizationDenied):
        await pending.repository.create_saved_chat(pending.decision(), uuid4())

    inactive = SavedChatFixture(active=False)
    with pytest.raises(AuthorizationDenied):
        await inactive.repository.create_saved_chat(inactive.decision(), uuid4())

    changed = SavedChatFixture(version=POLICY_VERSION + 1)
    with pytest.raises(AppWriteConflict):
        await changed.repository.create_saved_chat(
            changed.decision().model_copy(update={"policy_version": POLICY_VERSION}),
            uuid4(),
        )


@pytest.mark.asyncio
async def test_list_pages_only_safe_owned_active_unexpired_chats_in_stable_order():
    fixture = SavedChatFixture()
    older = make_chat(
        fixture.owner_id,
        uuid4(),
        created_at=NOW - timedelta(hours=2),
    )
    newest = make_chat(fixture.owner_id, uuid4())
    same_time_later_id = make_chat(fixture.owner_id, uuid4())
    other = make_chat(uuid4(), uuid4(), created_at=NOW + timedelta(minutes=1))
    deleting = make_chat(
        fixture.owner_id,
        uuid4(),
        created_at=NOW + timedelta(minutes=2),
        status="deleting",
    )
    expired = make_chat(
        fixture.owner_id,
        uuid4(),
        created_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(seconds=1),
    )
    # Same timestamp tie ordering is determined by the stable descending UUID string.
    for record in (older, newest, same_time_later_id, other, deleting, expired):
        fixture.chats.documents[str(record.id)] = record.document()

    first = await fixture.repository.list_saved_chats(
        fixture.decision(action="history"), page_size=2
    )
    second = await fixture.repository.list_saved_chats(
        fixture.decision(action="history"),
        cursor=first.next_cursor,
        page_size=2,
    )

    expected = sorted(
        (newest, same_time_later_id, older),
        key=lambda chat: (chat.last_activity_at, str(chat.id)),
        reverse=True,
    )
    assert [chat.id for chat in first.chats + second.chats] == [
        chat.id for chat in expected
    ]
    assert first.next_cursor is not None
    assert second.next_cursor is None
    assert all(
        chat.title == "New chat" and chat.version == 0
        for chat in first.chats + second.chats
    )
    assert all(
        set(chat.model_dump()) == {"id", "title", "version", "last_activity_at"}
        for chat in first.chats + second.chats
    )
    assert fixture.chats.cursors[0].closed == fixture.chats.cursors[1].closed == 1
    assert (
        fixture.chats.cursors[0].page_limit == fixture.chats.cursors[1].page_limit == 3
    )


@pytest.mark.asyncio
async def test_list_fails_closed_when_cursor_key_is_missing_or_cursor_is_cross_owner():
    fixture = SavedChatFixture()
    fixture.repository = ChatRepository(fixture.app_database, None, clock=lambda: NOW)
    for _ in range(2):
        record = make_chat(fixture.owner_id, uuid4(), created_at=NOW)
        fixture.chats.documents[str(record.id)] = record.document()

    with pytest.raises(CursorUnavailable):
        await fixture.repository.list_saved_chats(
            fixture.decision(action="history"), page_size=1
        )

    owner_cursor = fixture.codec.encode_chat_page(
        ChatSessionPosition(
            owner_user_id=uuid4(),
            policy_version=POLICY_VERSION,
            page_size=1,
            last_activity_at=NOW,
            session_id=uuid4(),
        )
    )
    fixture.repository = ChatRepository(
        fixture.app_database,
        fixture.codec,
        clock=lambda: NOW,
    )
    with pytest.raises(CursorRejected):
        await fixture.repository.list_saved_chats(
            fixture.decision(action="history"),
            cursor=owner_cursor,
            page_size=1,
        )


@pytest.mark.asyncio
async def test_explicit_index_initialization_creates_only_app_chat_indexes():
    fixture = SavedChatFixture()
    await fixture.app_database.initialize_chat_indexes()

    indexes = fixture.chats.indexes
    assert len(indexes) == 3
    assert any(options.get("unique") is True for _, options in indexes)
    assert all("expireAfterSeconds" not in options for _, options in indexes)
    assert set(fixture.client.database.collections) <= {
        "app_settings",
        "users",
        "roles",
        "chat_sessions",
    }
