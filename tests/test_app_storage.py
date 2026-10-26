"""App storage contracts. Fake transactions do not prove MongoDB atomicity."""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.models import (
    MAX_APP_DOCUMENT_BYTES,
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
    normalize_uuid,
    utc_datetime,
    validate_bson_document,
)
from goodshowroom_ai.storage.mongo import (
    APP_DATABASE_NAME,
    AppDatabase,
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
    RequiredAuditMissing,
)


class FakeDatabase:
    def __init__(self):
        self.collections = {}
        self.options = None

    def with_options(self, **options):
        self.options = options
        return self

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(name))


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.indexes = []
        self.writes = []
        self.matched_count = 1
        self.fail_insert = False
        self.fail_index = False

    async def create_index(self, keys, **options):
        if self.fail_index:
            raise RuntimeError("private driver error")
        self.indexes.append((tuple(keys), options))
        return options.get("name", "test-index")

    async def update_one(self, query, update, *, session):
        self.writes.append(("update", query, update, session))
        return SimpleNamespace(matched_count=self.matched_count)

    async def insert_one(self, document, *, session):
        self.writes.append(("insert", document, session))
        if self.fail_insert:
            raise RuntimeError("private driver error")
        return SimpleNamespace(inserted_id=document["_id"])


class FakeTransaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("transaction-start")
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        self.events.append("transaction-abort" if exc_type else "transaction-commit")
        return False


class FakeSession:
    def __init__(self, events):
        self.events = events

    def bind(self):
        return FakeSessionContext(self)

    async def start_transaction(self, **_options):
        return FakeTransaction(self.events)


class FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.events.append("session-start")
        return self.session

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.session.events.append("session-close")
        return False


class FakeAdmin:
    def __init__(self, events):
        self.events = events

    async def command(self, name):
        self.events.append(("admin-command", name))


class FakeClient:
    def __init__(self, events):
        self.events = events
        self.admin = FakeAdmin(events)
        self.database = FakeDatabase()
        self.selected_databases = []
        self.closed = 0
        self.session = FakeSession(events)

    def __getitem__(self, name):
        self.selected_databases.append(name)
        return self.database

    def start_session(self):
        return self.session

    async def close(self):
        self.closed += 1
        self.events.append("client-close")


def fake_factory(events, seen):
    def create(uri, **options):
        seen.append((uri, options))
        client = FakeClient(events)
        seen.append(client)
        return client

    return create


def authorized_context(actor_id):
    return InternalAuthorizationContext(
        actor_id=actor_id,
        capabilities=frozenset(
            {
                StorageCapability.ADVANCE_ACCESS_CONTROL,
                StorageCapability.WRITE_REQUIRED_AUDIT,
            }
        ),
    )


def audit_event(actor_id, **overrides):
    values = {
        "actor_id": actor_id,
        "target_id": uuid4(),
        "action": "access_control_change",
        "outcome": "success",
        "request_id": uuid4(),
        "created_at": datetime(2026, 10, 22, tzinfo=UTC),
    }
    return AppAuditEvent(**(values | overrides))


@pytest.mark.asyncio
async def test_missing_uri_fails_before_client_creation():
    calls = []

    def factory(*_args, **_kwargs):
        calls.append("created")
        raise AssertionError("client factory must not run")

    with pytest.raises(AppDatabaseUnavailable, match="not configured"):
        await AppDatabase.connect(None, client_factory=factory)
    assert calls == []


@pytest.mark.asyncio
async def test_connect_uses_fixed_database_and_closes_once():
    events, seen = [], []
    database = await AppDatabase.connect(
        SecretStr("mongodb://test.invalid"),
        client_factory=fake_factory(events, seen),
    )
    client = seen[1]
    assert client.selected_databases == [APP_DATABASE_NAME]
    assert APP_DATABASE_NAME == "byd_ai_layer"
    assert seen[0][1] == {"tz_aware": True, "serverSelectionTimeoutMS": 5000}
    assert events == [("admin-command", "ping")]
    await database.close()
    await database.close()
    assert client.closed == 1


@pytest.mark.asyncio
async def test_default_app_lifecycle_does_not_connect(test_settings):
    events, seen = [], []
    application = create_app(test_settings, client_factory=fake_factory(events, seen))
    async with application.router.lifespan_context(application):
        assert application.state.app_database is None
    assert seen == []
    assert events == []


@pytest.mark.asyncio
async def test_opt_in_app_lifecycle_connects_then_closes():
    events, seen = [], []
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_mongo_uri=SecretStr("mongodb://test.invalid"),
    )
    application = create_app(
        settings,
        connect_app_database=True,
        client_factory=fake_factory(events, seen),
    )
    async with application.router.lifespan_context(application):
        assert application.state.app_database is not None
        assert events == [("admin-command", "ping")]
    assert application.state.app_database is None
    assert seen[1].closed == 1


def test_app_storage_uri_uses_only_the_ai_layer_setting():
    app_settings = Settings(_env_file=None, AI_LAYER_MONGO_URI="mongodb://app.invalid")
    operational_settings = Settings(
        _env_file=None, MONGO_URI="mongodb://source.invalid"
    )
    assert app_settings.app_mongo_uri.get_secret_value() == "mongodb://app.invalid"
    assert operational_settings.app_mongo_uri is None


@pytest.mark.asyncio
async def test_opt_in_lifecycle_fails_before_client_creation_without_uri(test_settings):
    events, seen = [], []
    application = create_app(
        test_settings,
        connect_app_database=True,
        client_factory=fake_factory(events, seen),
    )
    with pytest.raises(AppDatabaseUnavailable, match="not configured"):
        async with application.router.lifespan_context(application):
            pytest.fail("lifespan must fail before serving")
    assert seen == []
    assert events == []


@pytest.mark.asyncio
async def test_index_initializer_only_uses_approved_app_collections():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    client = seen[1]
    await database.initialize_indexes()
    assert client.selected_databases == [APP_DATABASE_NAME]
    assert set(client.database.collections) == {"audit_events"}
    indexes = {
        (collection, options["name"]): options
        for collection, fake_collection in client.database.collections.items()
        for _keys, options in fake_collection.indexes
    }
    assert set(indexes) == {
        ("audit_events", "ix_actor_created"),
        ("audit_events", "ix_target_created"),
    }
    assert all(
        not hasattr(collection, "drop_index")
        for collection in client.database.collections.values()
    )


@pytest.mark.asyncio
async def test_default_authorization_context_denies_before_session_start():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    with pytest.raises(AuthorizationDenied):
        async with database.transaction(InternalAuthorizationContext()):
            pytest.fail("denied context must not start a write")
    assert events == [("admin-command", "ping")]


def test_internal_context_rejects_untyped_actor_and_capability():
    with pytest.raises(ValidationError):
        InternalAuthorizationContext(actor_id=str(uuid4()))
    with pytest.raises(ValidationError):
        InternalAuthorizationContext(
            actor_id=uuid4(), capabilities=frozenset({"write_required_audit"})
        )


@pytest.mark.asyncio
async def test_fence_and_audit_use_one_session_and_commit_together():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    client = seen[1]
    actor_id = uuid4()
    async with database.transaction(authorized_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(4)
        await transaction.append_required_audit(audit_event(actor_id))
    settings_write = client.database.collections["app_settings"].writes[0]
    audit_write = client.database.collections["audit_events"].writes[0]
    assert settings_write[1] == {"_id": "access_control", "policy_version": 4}
    assert settings_write[2] == {"$inc": {"commit_fence": 1}}
    assert settings_write[3] is audit_write[2]
    assert audit_write[1]["actor_id"] == str(actor_id)
    assert audit_write[1]["request_id"]
    assert events[-3:] == [
        "transaction-start",
        "transaction-commit",
        "session-close",
    ]


@pytest.mark.asyncio
async def test_policy_conflict_aborts_and_closes_session():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    client = seen[1]
    client.database["app_settings"].matched_count = 0
    with pytest.raises(AppWriteConflict, match="policy changed"):
        async with database.transaction(authorized_context(uuid4())) as transaction:
            await transaction.advance_access_control_fence(2)
    assert client.database["audit_events"].writes == []
    assert events[-3:] == [
        "transaction-start",
        "transaction-abort",
        "session-close",
    ]


@pytest.mark.asyncio
async def test_audit_failure_aborts_and_closes_session():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    client = seen[1]
    client.database["audit_events"].fail_insert = True
    actor_id = uuid4()
    with pytest.raises(AppDatabaseUnavailable, match="Required audit write failed"):
        async with database.transaction(authorized_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(1)
            await transaction.append_required_audit(audit_event(actor_id))
    assert len(client.database["app_settings"].writes) == 1
    assert len(client.database["audit_events"].writes) == 1
    assert events[-3:] == [
        "transaction-start",
        "transaction-abort",
        "session-close",
    ]


@pytest.mark.asyncio
async def test_fence_without_required_audit_aborts():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    actor_id = uuid4()
    with pytest.raises(RequiredAuditMissing):
        async with database.transaction(authorized_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(1)
    assert events[-3:] == [
        "transaction-start",
        "transaction-abort",
        "session-close",
    ]


@pytest.mark.asyncio
async def test_audit_actor_must_match_internal_context():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    actor_id = uuid4()
    with pytest.raises(AuthorizationDenied):
        async with database.transaction(authorized_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(1)
            await transaction.append_required_audit(audit_event(uuid4()))
    assert seen[1].database.collections.get("audit_events") is None
    assert events[-2:] == ["transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_caught_audit_failure_still_poisons_transaction():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    client = seen[1]
    client.database["audit_events"].fail_insert = True
    actor_id = uuid4()
    with pytest.raises(AppDatabaseUnavailable, match="not safe to commit"):
        async with database.transaction(authorized_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(1)
            try:
                await transaction.append_required_audit(audit_event(actor_id))
            except AppDatabaseUnavailable:
                pass
    assert events[-3:] == [
        "transaction-start",
        "transaction-abort",
        "session-close",
    ]


@pytest.mark.asyncio
async def test_transaction_handle_cannot_write_after_context_exit():
    events, seen = [], []
    database = await AppDatabase.connect(
        "mongodb://test.invalid", client_factory=fake_factory(events, seen)
    )
    context = InternalAuthorizationContext(
        actor_id=uuid4(),
        capabilities=frozenset({StorageCapability.WRITE_REQUIRED_AUDIT}),
    )
    async with database.transaction(context) as transaction:
        pass
    with pytest.raises(AppDatabaseUnavailable, match="no longer active"):
        await transaction.append_required_audit(audit_event(context.actor_id))
    assert seen[1].database.collections.get("audit_events") is None


@pytest.mark.parametrize(
    "value", ["", "not-a-uuid", "{00000000-0000-0000-0000-000000000001}"]
)
def test_uuid_requires_canonical_hyphenated_string(value):
    with pytest.raises(ValueError, match="canonical UUID"):
        normalize_uuid(value)


def test_uuid_accepts_uuid_objects_and_canonical_strings():
    value = uuid4()
    assert normalize_uuid(value) == str(value)
    assert normalize_uuid(str(value)) == str(value)


def test_utc_datetime_requires_awareness_and_normalizes_offset():
    with pytest.raises(ValueError, match="UTC offset"):
        utc_datetime(datetime(2026, 10, 22))
    source = datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=5)))
    assert utc_datetime(source) == datetime(2026, 10, 22, 7, tzinfo=UTC)


def test_bson_document_size_is_measured_after_encoding():
    assert validate_bson_document({"value": "x"}, max_bytes=20) < 20
    with pytest.raises(ValueError, match="size limit"):
        validate_bson_document({"value": "x" * 64}, max_bytes=32)
    with pytest.raises(ValueError, match="size limit"):
        validate_bson_document({"value": "x" * MAX_APP_DOCUMENT_BYTES})


def test_bson_document_rejects_naive_nested_dates_and_unsupported_values():
    with pytest.raises(ValueError, match="UTC offset"):
        validate_bson_document({"nested": [{"at": datetime(2026, 10, 22)}]})
    with pytest.raises(ValueError, match="not BSON encodable"):
        validate_bson_document({"unsupported": object()})
    with pytest.raises(ValueError, match="non-finite"):
        validate_bson_document({"unsupported": float("inf")})


def test_bson_document_rejects_cycles_and_excessive_limits():
    document = {}
    document["self"] = document
    with pytest.raises(ValueError, match="circular"):
        validate_bson_document(document)
    with pytest.raises(ValueError, match="size limit is invalid"):
        validate_bson_document({"value": "safe"}, max_bytes=MAX_APP_DOCUMENT_BYTES + 1)


def test_audit_model_rejects_free_text_and_normalizes_date():
    actor_id = uuid4()
    event = audit_event(
        actor_id,
        created_at=datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=5))),
    )
    document = event.to_bson_document()
    assert document["created_at"] == datetime(2026, 10, 22, 7, tzinfo=UTC)
    assert document["schema_version"] == 1
    with pytest.raises(ValidationError):
        AppAuditEvent(**event.model_dump(), message="not permitted")
