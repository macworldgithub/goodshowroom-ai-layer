"""Offline storage contracts for TICKET-023 access requests."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from bson import BSON
from bson.codec_options import CodecOptions
from pydantic import ValidationError

from goodshowroom_ai.storage.access_requests import AccessRequestRecord
from goodshowroom_ai.storage.identities import Grant, RoleRecord
from goodshowroom_ai.storage.models import (
    InternalAuthorizationContext,
    StorageCapability,
)
from goodshowroom_ai.storage.mongo import (
    APP_DATABASE_NAME,
    AppDatabase,
    AppWriteConflict,
    AuthorizationDenied,
)
from tests.test_identity_storage import (
    NOW,
    FakeClient,
    FakeCollection,
    FakeDatabase,
    audit,
    full_context,
    role_record,
    selected_grant,
    user_record,
)


class AccessRequestCursor:
    def __init__(self, collection, query, session):
        self.collection = collection
        self.query = deepcopy(query)
        self.session = session
        self.sort_key = None
        self.sort_direction = 1
        self.limit_value = None
        self.max_time_value = None
        self.list_length = None
        collection.find_calls.append(self)

    def sort(self, key, direction):
        self.sort_key = key
        self.sort_direction = direction
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def max_time_ms(self, value):
        self.max_time_value = value
        return self

    async def to_list(self, *, length):
        assert self.session is not None
        self.list_length = length
        rows = [
            deepcopy(document)
            for document in self.collection.documents.values()
            if self.collection.matches(document, self.query)
        ]
        if self.sort_key is not None:
            rows.sort(
                key=lambda row: row[self.sort_key],
                reverse=self.sort_direction < 0,
            )
        return rows[: min(length, self.limit_value or length)]


class AccessRequestFakeCollection(FakeCollection):
    def __init__(self, name):
        super().__init__(name)
        self.find_calls = []
        self.race_document = None

    @staticmethod
    def matches(document, query):
        for key, expected in query.items():
            actual = document.get(key)
            if isinstance(expected, dict) and "$gt" in expected:
                if actual <= expected["$gt"]:
                    return False
            elif actual != expected:
                return False
        return True

    def _has_unique_collision(self, document, *, exclude_id=None):
        for keys, options in self.indexes:
            if not options.get("unique"):
                continue
            partial = options.get("partialFilterExpression", {})
            if not self.matches(document, partial):
                continue
            names = [name for name, _direction in keys]
            for existing_id, existing in self.documents.items():
                if existing_id == exclude_id or not self.matches(existing, partial):
                    continue
                if all(existing.get(name) == document.get(name) for name in names):
                    return True
        return False

    async def insert_one(self, document, *, session):
        if self.race_document is not None:
            competing = self.race_document
            self.race_document = None
            await FakeCollection.insert_one(self, competing, session=session)
        return await FakeCollection.insert_one(self, document, session=session)

    def find(self, query, *, session):
        assert session is not None
        return AccessRequestCursor(self, query, session)

    async def find_one(self, query, *, session):
        assert session is not None
        for document in self.documents.values():
            if self.matches(document, query):
                return deepcopy(document)
        return None


class AccessRequestFakeDatabase(FakeDatabase):
    def __init__(self):
        super().__init__()
        self.collections["access_requests"] = AccessRequestFakeCollection(
            "access_requests"
        )


def request_context(actor_id, *, capabilities=None):
    if capabilities is None:
        capabilities = {
            StorageCapability.READ_IDENTITIES,
            StorageCapability.WRITE_IDENTITIES,
            StorageCapability.READ_ACCESS_REQUESTS,
            StorageCapability.WRITE_ACCESS_REQUESTS,
            StorageCapability.ADVANCE_ACCESS_CONTROL,
            StorageCapability.WRITE_REQUIRED_AUDIT,
        }
    return InternalAuthorizationContext(
        actor_id=actor_id, capabilities=frozenset(capabilities)
    )


async def connect_access_fake():
    events, clients = [], []

    def factory(_uri, **_options):
        client = FakeClient(events)
        client.database = AccessRequestFakeDatabase()
        clients.append(client)
        return client

    database = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=factory
    )
    await database.initialize_identity_indexes()
    await database.initialize_access_request_indexes()
    return database, clients[0], events


def current_policy_version(client):
    return client.database["app_settings"].documents["access_control"]["policy_version"]


async def fenced_access_write(database, client, actor_id, target_id, callback):
    context = request_context(actor_id)
    async with database.transaction(context) as transaction:
        await transaction.advance_access_control_fence(current_policy_version(client))
        result = await callback(transaction)
        await transaction.append_required_audit(audit(actor_id, target_id))
    return result


async def save_user(database, client, user):
    async def insert(transaction):
        await transaction.create_user(user)

    async with database.transaction(full_context(user.id)) as transaction:
        await transaction.advance_access_control_fence(current_policy_version(client))
        await insert(transaction)
        await transaction.append_required_audit(audit(user.id, user.id))


async def save_role(database, client, actor_id, role):
    async with database.transaction(full_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(current_policy_version(client))
        await transaction.create_role(role)
        await transaction.append_required_audit(audit(actor_id, role.id))


def access_request(user, *, identifier=None, email_key=None, expires_at=None):
    return AccessRequestRecord(
        id=identifier or uuid4(),
        kind="request",
        email_key=email_key or user.email_key,
        user_id=user.id,
        expires_at=expires_at,
        created_at=NOW,
        updated_at=NOW,
    )


def preapproval(email_key, role, *, scope=None, identifier=None, expires_at=None):
    return AccessRequestRecord(
        id=identifier or uuid4(),
        kind="preapproval",
        email_key=email_key,
        role_id=role.id,
        scope_limit=(scope or selected_grant(),),
        expires_at=expires_at,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_access_request_indexes_are_explicit_and_partial():
    database, client, _events = await connect_access_fake()
    collection = client.database["access_requests"]
    indexes = {options["name"]: (keys, options) for keys, options in collection.indexes}

    assert set(indexes) == {"uq_active_kind_email", "ix_status_created"}
    assert indexes["uq_active_kind_email"][0] == (
        ("kind", 1),
        ("email_key", 1),
    )
    assert indexes["uq_active_kind_email"][1]["unique"] is True
    assert indexes["uq_active_kind_email"][1]["partialFilterExpression"] == {
        "active": True
    }
    assert indexes["ix_status_created"][0] == (
        ("status", 1),
        ("created_at", 1),
    )
    assert client.selected_databases == [APP_DATABASE_NAME]
    assert set(client.database.collections) == {
        "app_settings",
        "users",
        "roles",
        "access_requests",
    }
    assert not collection.writes
    await database.close()


def test_access_request_model_is_strict_versionless_and_bson_dates_round_trip():
    user = user_record()
    record = access_request(user, expires_at=NOW + timedelta(minutes=1))
    document = record.document()

    assert "version" not in AccessRequestRecord.model_fields
    assert "version" not in document
    assert document["_id"] == str(record.id)
    assert document["created_at"] == NOW
    decoded = BSON.encode(document).decode(
        codec_options=CodecOptions(tz_aware=True, tzinfo=UTC)
    )
    identifier = decoded.pop("_id")
    decoded["id"] = UUID(identifier)
    decoded["user_id"] = UUID(decoded["user_id"])
    decoded["scope_limit"] = tuple(
        Grant.model_validate(grant) for grant in decoded["scope_limit"]
    )
    assert AccessRequestRecord.model_validate(decoded) == record
    assert decoded["expires_at"].utcoffset() == timedelta(0)

    with pytest.raises(ValidationError):
        AccessRequestRecord.model_validate(document | {"version": 0})
    with pytest.raises(ValidationError):
        AccessRequestRecord.model_validate(document | {"extra": "forbidden"})
    with pytest.raises(ValidationError):
        access_request(user, expires_at=datetime(2026, 10, 23, 9))
    with pytest.raises(ValidationError):
        AccessRequestRecord.model_validate(
            preapproval(
                "staff@example.com",
                role_record(),
                scope=selected_grant(),
            ).model_dump()
            | {"user_id": uuid4()}
        )


@pytest.mark.asyncio
async def test_request_read_scope_pagination_bounds_and_max_time():
    database, client, _events = await connect_access_fake()
    collection = client.database["access_requests"]
    records = []
    for number, status in (
        (1, "pending"),
        (2, "rejected"),
        (3, "pending"),
        (4, "pending"),
    ):
        user = user_record(
            id=UUID(int=100 + number), email_key=f"staff{number}@example.com"
        )
        record = AccessRequestRecord(
            id=UUID(int=number),
            kind="request",
            email_key=user.email_key,
            user_id=user.id,
            status=status,
            active=status == "pending",
            reviewed_by=uuid4() if status != "pending" else None,
            reviewed_at=NOW if status != "pending" else None,
            created_at=NOW,
            updated_at=NOW,
        )
        collection.documents[str(record.id)] = record.document()
        records.append(record)

    reader = request_context(
        uuid4(), capabilities={StorageCapability.READ_ACCESS_REQUESTS}
    )
    async with database.transaction(reader) as transaction:
        first_page = await transaction.list_access_requests(status="pending", limit=2)
        second_page = await transaction.list_access_requests(
            status="pending", limit=2, after_id=UUID(int=2)
        )
        loaded = await transaction.get_access_request(UUID(int=1))

    assert tuple(item.id for item in first_page) == (UUID(int=1), UUID(int=3))
    assert tuple(item.id for item in second_page) == (UUID(int=3), UUID(int=4))
    assert loaded == records[0]
    assert collection.find_calls[0].query == {"status": "pending"}
    assert collection.find_calls[0].sort_key == "_id"
    assert collection.find_calls[0].sort_direction == 1
    assert collection.find_calls[0].limit_value == 2
    assert collection.find_calls[0].max_time_value == 5000
    assert collection.find_calls[0].list_length == 2
    assert collection.find_calls[1].query == {
        "status": "pending",
        "_id": {"$gt": str(UUID(int=2))},
    }
    calls_before_invalid_pages = len(collection.find_calls)
    for status, limit, after_id in (
        ("unknown", 1, None),
        ("pending", 0, None),
        ("pending", 101, None),
        ("pending", True, None),
        ("pending", 1, "not-a-uuid"),
    ):
        with pytest.raises(ValueError, match="Invalid access request page"):
            async with database.transaction(reader) as transaction:
                await transaction.list_access_requests(
                    status=status, limit=limit, after_id=after_id
                )
    assert len(collection.find_calls) == calls_before_invalid_pages

    no_read = request_context(uuid4(), capabilities={StorageCapability.READ_IDENTITIES})
    with pytest.raises(AuthorizationDenied):
        async with database.transaction(no_read) as transaction:
            await transaction.get_access_request(UUID(int=1))


@pytest.mark.asyncio
async def test_request_creation_requires_fence_and_required_audit():
    database, client, events = await connect_access_fake()
    user = user_record()
    await save_user(database, client, user)
    record = access_request(user)
    collection = client.database["access_requests"]
    context = request_context(user.id)

    with pytest.raises(ValueError, match="fence before audit"):
        async with database.transaction(context) as transaction:
            await transaction.create_access_request(record, NOW)
    assert not collection.writes
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]

    read_only = request_context(
        user.id,
        capabilities={
            StorageCapability.READ_ACCESS_REQUESTS,
            StorageCapability.ADVANCE_ACCESS_CONTROL,
            StorageCapability.WRITE_REQUIRED_AUDIT,
        },
    )
    with pytest.raises(AuthorizationDenied):
        async with database.transaction(read_only) as transaction:
            await transaction.advance_access_control_fence(
                current_policy_version(client)
            )
            await transaction.create_access_request(record, NOW)
    assert not collection.documents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["missing", "actor_mismatch", "email_mismatch", "not_pending"]
)
async def test_request_creation_rejects_ineligible_user_parent(case):
    database, client, _events = await connect_access_fake()
    user = user_record()
    if case != "missing":
        await save_user(database, client, user)
    if case == "not_pending":
        client.database["users"].documents[str(user.id)]["approval_status"] = "rejected"
    actor_id = uuid4() if case == "actor_mismatch" else user.id
    email_key = "different@example.com" if case == "email_mismatch" else user.email_key
    record = access_request(user, email_key=email_key)
    if case == "missing":
        record = access_request(
            user_record(id=uuid4(), email_key="missing@example.com")
        )
        actor_id = record.user_id

    async def create(transaction):
        return await transaction.create_access_request(record, NOW)

    with pytest.raises(ValueError, match="Request parent is not eligible"):
        await fenced_access_write(database, client, actor_id, record.id, create)
    assert not client.database["access_requests"].documents


@pytest.mark.asyncio
async def test_preapproval_requires_active_role_narrow_scope_and_no_user_row():
    database, client, _events = await connect_access_fake()
    admin_id = uuid4()
    allowed = selected_grant()
    narrow = Grant(
        dataset=allowed.dataset,
        sites=allowed.sites,
        record_mode=allowed.record_mode,
        selected_records=allowed.selected_records,
        fields=("site",),
    )
    role = role_record(active=True, grants=(allowed,))
    await save_role(database, client, admin_id, role)
    record = preapproval("preapproved@example.com", role, scope=narrow)

    async def create(transaction):
        return await transaction.create_access_request(record, NOW)

    created = await fenced_access_write(database, client, admin_id, record.id, create)
    assert created == record
    assert created.user_id is None
    assert client.database["users"].documents == {}
    assert (
        client.database["access_requests"].documents[str(record.id)]["user_id"] is None
    )

    for bad_role, bad_scope in (
        (None, narrow),
        (role_record(active=False, grants=(allowed,)), narrow),
        (
            role,
            Grant(
                dataset="byd-panel",
                sites=("site_a",),
                record_mode="all_permitted",
                fields=("site",),
            ),
        ),
    ):
        if bad_role is not None and bad_role.id != role.id:
            bad_role = RoleRecord(
                **(bad_role.model_dump() | {"name_key": f"role_{uuid4().hex}"})
            )
            await save_role(database, client, admin_id, bad_role)
        invalid = preapproval(
            f"{uuid4()}@example.com",
            bad_role or role_record(),
            scope=bad_scope,
        )

        async def create_invalid(transaction):
            return await transaction.create_access_request(invalid, NOW)

        with pytest.raises(
            ValueError, match="Preapproval role or scope is not permitted"
        ):
            await fenced_access_write(
                database, client, admin_id, invalid.id, create_invalid
            )
        assert str(invalid.id) not in client.database["access_requests"].documents


@pytest.mark.asyncio
async def test_request_expiry_is_strict_at_now_and_one_policy_bump_per_write_batch():
    database, client, _events = await connect_access_fake()
    actor_id = uuid4()
    role = role_record(active=True)
    await save_role(database, client, actor_id, role)
    at_now = preapproval("expires-now@example.com", role, expires_at=NOW)
    assert at_now.available(NOW) is False
    with pytest.raises(ValueError, match="available and pending"):

        async def create_expired(transaction):
            await transaction.create_access_request(at_now, NOW)

        await fenced_access_write(database, client, actor_id, at_now.id, create_expired)

    future = NOW + timedelta(microseconds=1)
    first = preapproval("first@example.com", role, expires_at=future)
    second = preapproval("second@example.com", role, expires_at=future)
    before = current_policy_version(client)

    async def create_pair(transaction):
        await transaction.create_access_request(first, NOW)
        await transaction.create_access_request(second, NOW)

    await fenced_access_write(database, client, actor_id, first.id, create_pair)
    assert first.available(NOW) is True
    assert first.available(future) is False
    assert current_policy_version(client) == before + 1
    increments = [
        write
        for write in client.database["app_settings"].writes
        if write[0] == "update" and write[2] == {"$inc": {"policy_version": 1}}
    ]
    assert len(increments) == 2  # one role setup update, one two-record transaction


@pytest.mark.asyncio
async def test_pending_duplicate_returns_existing_but_closed_history_blocks_resubmit():
    database, client, _events = await connect_access_fake()
    user = user_record()
    await save_user(database, client, user)
    original = access_request(user)

    async def create_original(transaction):
        return await transaction.create_access_request(original, NOW)

    await fenced_access_write(database, client, user.id, original.id, create_original)
    duplicate = access_request(user)
    before_duplicate = current_policy_version(client)

    async def create_duplicate(transaction):
        return await transaction.create_access_request(duplicate, NOW)

    returned = await fenced_access_write(
        database, client, user.id, duplicate.id, create_duplicate
    )
    assert returned == original
    assert current_policy_version(client) == before_duplicate
    assert len(client.database["access_requests"].documents) == 1

    before_close = current_policy_version(client)

    async def reject(transaction):
        await transaction.close_access_request(
            original.id, status="rejected", now=NOW + timedelta(minutes=1)
        )

    await fenced_access_write(database, client, user.id, original.id, reject)
    closed = client.database["access_requests"].documents[str(original.id)]
    assert closed["status"] == "rejected"
    assert closed["active"] is False
    assert closed["reviewed_by"] == str(user.id)
    assert closed["reviewed_at"] == NOW + timedelta(minutes=1)
    assert current_policy_version(client) == before_close + 1

    replacement = access_request(user)

    async def resubmit(transaction):
        return await transaction.create_access_request(replacement, NOW)

    with pytest.raises(
        AppWriteConflict, match="Resubmission requires an approved policy"
    ):
        await fenced_access_write(database, client, user.id, replacement.id, resubmit)
    assert len(client.database["access_requests"].documents) == 1


@pytest.mark.asyncio
async def test_duplicate_key_race_aborts_and_recovers_in_a_new_transaction():
    database, client, events = await connect_access_fake()
    user = user_record()
    await save_user(database, client, user)
    wanted = access_request(user)
    competing = access_request(user)
    collection = client.database["access_requests"]
    collection.race_document = competing.document()
    start = len(events)

    async def create(transaction):
        return await transaction.create_access_request(wanted, NOW)

    with pytest.raises(AppWriteConflict, match="retry in a new transaction"):
        await fenced_access_write(database, client, user.id, wanted.id, create)
    assert events[start:] == [
        "session-start",
        "transaction-start",
        "transaction-abort",
        "session-close",
    ]
    assert set(collection.documents) == {str(competing.id)}

    async def retry(transaction):
        return await transaction.create_access_request(wanted, NOW)

    recovered = await fenced_access_write(database, client, user.id, wanted.id, retry)
    assert recovered.id == competing.id


@pytest.mark.asyncio
async def test_close_rejects_workflow_transitions_and_expired_or_repeated_requests():
    database, client, _events = await connect_access_fake()
    user = user_record()
    await save_user(database, client, user)
    expired = access_request(user, expires_at=NOW)
    client.database["access_requests"].documents[str(expired.id)] = expired.document()

    for status in ("approved", "consumed", "pending"):

        async def invalid_status(transaction, status=status):
            await transaction.close_access_request(expired.id, status=status, now=NOW)

        with pytest.raises(ValueError, match="service workflow"):
            await fenced_access_write(
                database, client, user.id, expired.id, invalid_status
            )

    async def close_expired(transaction):
        await transaction.close_access_request(expired.id, status="rejected", now=NOW)

    with pytest.raises(AppWriteConflict, match="not available"):
        await fenced_access_write(database, client, user.id, expired.id, close_expired)

    role = role_record(active=True)
    await save_role(database, client, user.id, role)
    unused = preapproval("unused@example.com", role)

    async def create_preapproval(transaction):
        await transaction.create_access_request(unused, NOW)

    await fenced_access_write(database, client, user.id, unused.id, create_preapproval)

    async def reject_preapproval(transaction):
        await transaction.close_access_request(unused.id, status="rejected", now=NOW)

    with pytest.raises(AppWriteConflict, match="not available"):
        await fenced_access_write(
            database, client, user.id, unused.id, reject_preapproval
        )

    async def revoke_preapproval(transaction):
        await transaction.close_access_request(unused.id, status="revoked", now=NOW)

    await fenced_access_write(database, client, user.id, unused.id, revoke_preapproval)
    assert client.database["access_requests"].documents[str(unused.id)]["status"] == (
        "revoked"
    )


@pytest.mark.asyncio
async def test_access_request_partial_unique_index_allows_inactive_history():
    database_collection = AccessRequestFakeCollection("access_requests")
    database_collection.indexes.append(
        (
            (("kind", 1), ("email_key", 1)),
            {
                "unique": True,
                "partialFilterExpression": {"active": True},
            },
        )
    )
    user = user_record()
    closed_one = AccessRequestRecord(
        **(
            access_request(user).model_dump()
            | {
                "status": "rejected",
                "active": False,
                "reviewed_by": uuid4(),
                "reviewed_at": NOW,
            }
        )
    )
    closed_two = AccessRequestRecord(
        **(
            access_request(user).model_dump()
            | {
                "status": "revoked",
                "active": False,
                "reviewed_by": uuid4(),
                "reviewed_at": NOW,
            }
        )
    )

    await database_collection.insert_one(closed_one.document(), session=object())
    await database_collection.insert_one(closed_two.document(), session=object())
    assert len(database_collection.documents) == 2
    assert all(
        document["active"] is False
        for document in database_collection.documents.values()
    )
