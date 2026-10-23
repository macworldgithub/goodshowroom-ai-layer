"""Offline contracts for the TICKET-022 identity repositories."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from pymongo.errors import DuplicateKeyError

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.identities import (
    Grant,
    RoleRecord,
    SourceReference,
    UserRecord,
)
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
)
from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.mongo import (
    APP_DATABASE_NAME,
    AppDatabase,
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
)


POLICY_VERSION = 7
NOW = datetime(2026, 10, 23, 9, tzinfo=UTC)


class FakeCollection:
    def __init__(self, name, documents=None):
        self.name = name
        self.documents = documents or {}
        self.indexes = []
        self.writes = []
        self.reject_policy_increment = False
        self.reject_replace = False
        self.fail_insert = False

    async def create_index(self, keys, **options):
        self.indexes.append((tuple(keys), options))
        return options.get("name", "fake-index")

    async def find_one(self, query, *, session):
        assert session is not None
        document = self.documents.get(query.get("_id"))
        if document is None or any(
            document.get(key) != value for key, value in query.items()
        ):
            return None
        return deepcopy(document)

    async def insert_one(self, document, *, session):
        assert session is not None
        self.writes.append(("insert", deepcopy(document), session))
        if self.fail_insert:
            raise RuntimeError("simulated insert failure")
        if document["_id"] in self.documents or self._has_unique_collision(document):
            raise DuplicateKeyError("simulated unique index collision")
        self.documents[document["_id"]] = deepcopy(document)
        return SimpleNamespace(inserted_id=document["_id"])

    async def replace_one(self, query, document, *, session):
        assert session is not None
        self.writes.append(("replace", deepcopy(query), deepcopy(document), session))
        current = self.documents.get(query.get("_id"))
        matched = current is not None and all(
            current.get(key) == value for key, value in query.items()
        )
        if self.reject_replace:
            matched = False
        if matched and self._has_unique_collision(document, exclude_id=document["_id"]):
            raise DuplicateKeyError("simulated unique index collision")
        if matched:
            self.documents[document["_id"]] = deepcopy(document)
        return SimpleNamespace(matched_count=int(matched))

    async def update_one(self, query, update, *, session):
        assert session is not None
        self.writes.append(("update", deepcopy(query), deepcopy(update), session))
        current = self.documents.get(query.get("_id"))
        matched = current is not None and all(
            current.get(key) == value for key, value in query.items()
        )
        increments = update.get("$inc", {})
        if self.reject_policy_increment and "policy_version" in increments:
            return SimpleNamespace(matched_count=0)
        if matched:
            for key, amount in increments.items():
                current[key] = current.get(key, 0) + amount
        return SimpleNamespace(matched_count=int(matched))

    def _has_unique_collision(self, document, *, exclude_id=None):
        for keys, options in self.indexes:
            if not options.get("unique"):
                continue
            names = [name for name, _direction in keys]
            for existing_id, existing in self.documents.items():
                if existing_id == exclude_id:
                    continue
                if all(existing.get(name) == document.get(name) for name in names):
                    return True
        return False


class FakeDatabase:
    def __init__(self):
        self.collections = {
            "app_settings": FakeCollection(
                "app_settings",
                {
                    "access_control": {
                        "_id": "access_control",
                        "policy_version": POLICY_VERSION,
                        "commit_fence": 0,
                    }
                },
            )
        }

    def with_options(self, **_options):
        return self

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(name))


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

    def start_transaction(self):
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


class FakeClient:
    def __init__(self, events):
        self.events = events
        self.admin = SimpleNamespace(command=self.command)
        self.database = FakeDatabase()
        self.selected_databases = []
        self.closed = 0
        self.session = FakeSession(events)

    async def command(self, name):
        self.events.append(("admin-command", name))

    def __getitem__(self, name):
        self.selected_databases.append(name)
        return self.database

    def start_session(self):
        return self.session

    async def close(self):
        self.closed += 1
        self.events.append("client-close")


def fake_factory(events, clients):
    def create(_uri, **_options):
        client = FakeClient(events)
        clients.append(client)
        return client

    return create


def full_context(actor_id=None):
    return InternalAuthorizationContext(
        actor_id=actor_id or uuid4(),
        capabilities=frozenset(
            {
                StorageCapability.READ_IDENTITIES,
                StorageCapability.WRITE_IDENTITIES,
                StorageCapability.ADVANCE_ACCESS_CONTROL,
                StorageCapability.WRITE_REQUIRED_AUDIT,
            }
        ),
    )


def login_context(actor_id, *, capabilities=None):
    granted = capabilities
    if granted is None:
        granted = {
            StorageCapability.READ_LOGINS,
            StorageCapability.WRITE_LOGINS,
            StorageCapability.ADVANCE_ACCESS_CONTROL,
            StorageCapability.WRITE_REQUIRED_AUDIT,
        }
    return InternalAuthorizationContext(
        actor_id=actor_id,
        capabilities=frozenset(granted),
    )


def selected_grant():
    return Grant(
        dataset="byd-panel",
        sites=("site_a",),
        record_mode="selected",
        selected_records=(
            SourceReference(
                database="byd-panel",
                collection="customers",
                key_field="customer_id",
                key_type="string",
                key_value="customer-42",
            ),
        ),
        fields=("customer_id", "site"),
        metrics=("sales_count",),
        tools=("customer_lookup",),
    )


def user_record(**overrides):
    values = {
        "id": uuid4(),
        "provider_account_id": "google-subject-1",
        "email_key": " Staff.Member+sales@Example.com ",
        "display_name": "Staff Member",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return UserRecord(**(values | overrides))


def role_record(**overrides):
    values = {
        "id": uuid4(),
        "name_key": "sales_viewer",
        "created_at": NOW,
        "updated_at": NOW,
        "grants": (selected_grant(),),
    }
    return RoleRecord(**(values | overrides))


def login_record(user_id, **overrides):
    values = {
        "id": uuid4(),
        "user_id": user_id,
        "generation": 0,
        "auth_started_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        "auth_expires_at": NOW + timedelta(hours=9),
        "expires_at": NOW + timedelta(hours=8),
        "lease_valid_until": NOW + timedelta(minutes=5),
    }
    return LoginRecord(**(values | overrides))


def audit(actor_id, target_id):
    return AppAuditEvent(
        actor_id=actor_id,
        target_id=target_id,
        action="access_control_change",
        outcome="success",
        request_id=uuid4(),
        created_at=NOW,
    )


async def connect_fake():
    events, clients = [], []
    database = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=fake_factory(events, clients)
    )
    return database, clients[0], events


async def fenced_write(database, actor_id, target_id, callback, policy_version=None):
    async with database.transaction(full_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(
            POLICY_VERSION if policy_version is None else policy_version
        )
        await callback(transaction)
        await transaction.append_required_audit(audit(actor_id, target_id))


@pytest.mark.asyncio
async def test_identity_indexes_are_explicit_and_app_only():
    events, clients = [], []
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_mongo_uri=SecretStr("mongodb://offline.invalid"),
    )
    application = create_app(
        settings,
        connect_app_database=True,
        client_factory=fake_factory(events, clients),
    )
    async with application.router.lifespan_context(application):
        client = clients[0]
        assert client.selected_databases == [APP_DATABASE_NAME]
        assert set(client.database.collections) == {"app_settings"}
        await application.state.app_database.initialize_identity_indexes()
        indexes = {
            (name, options["name"]): (keys, options)
            for name, collection in client.database.collections.items()
            for keys, options in collection.indexes
        }
        assert set(indexes) == {
            ("users", "uq_provider_account"),
            ("users", "uq_email_key"),
            ("users", "ix_approval_updated"),
            ("roles", "uq_name_key"),
        }
        assert indexes[("users", "uq_provider_account")][0] == (
            ("provider", 1),
            ("provider_account_id", 1),
        )
        assert indexes[("users", "uq_provider_account")][1]["unique"] is True
        assert indexes[("users", "uq_email_key")][0] == (("email_key", 1),)
        assert indexes[("users", "uq_email_key")][1]["unique"] is True
        assert indexes[("roles", "uq_name_key")][0] == (("name_key", 1),)
        assert indexes[("roles", "uq_name_key")][1]["unique"] is True
        assert set(client.database.collections) == {"app_settings", "users", "roles"}
    assert clients[0].closed == 1
    assert events[-1] == "client-close"


@pytest.mark.asyncio
async def test_pending_user_and_typed_role_round_trip_under_read_context():
    database, client, _events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    user, role = user_record(), role_record()

    async with database.transaction(full_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION)
        await transaction.create_role(role)
        await transaction.create_user(user)
        await transaction.append_required_audit(audit(actor_id, user.id))

    assert user.email_key == "staff.member+sales@example.com"
    assert user.provider_account_id == "google-subject-1"
    assert user.approval_status == "pending"
    assert user.role_id is None
    assert user.scope_limit == ()
    assert client.database["users"].documents[str(user.id)]["provider_account_id"] == (
        "google-subject-1"
    )
    assert client.database["users"].documents[str(user.id)]["email_key"] == (
        "staff.member+sales@example.com"
    )

    reader = InternalAuthorizationContext(
        actor_id=actor_id,
        capabilities=frozenset({StorageCapability.READ_IDENTITIES}),
    )
    async with database.transaction(reader) as transaction:
        loaded_user = await transaction.get_user(user.id)
        loaded_role = await transaction.get_role(role.id)

    assert loaded_user == user
    assert loaded_role == role
    assert loaded_role.grants[0].selected_records == role.grants[0].selected_records
    stored_role = client.database["roles"].documents[str(role.id)]
    assert stored_role["grants"][0]["selected_records"][0] == {
        "database": "byd-panel",
        "collection": "customers",
        "key_field": "customer_id",
        "key_type": "string",
        "key_value": "customer-42",
    }
    assert all(
        write[-1] is client.session
        for name in ("users", "roles", "audit_events", "app_settings")
        for write in client.database[name].writes
    )
    assert client.selected_databases == [APP_DATABASE_NAME]


@pytest.mark.asyncio
async def test_identity_capabilities_deny_reads_and_writes_without_storage_calls():
    database, client, _events = await connect_fake()
    user = user_record()
    context = InternalAuthorizationContext(
        actor_id=uuid4(),
        capabilities=frozenset({StorageCapability.WRITE_REQUIRED_AUDIT}),
    )
    with pytest.raises(AuthorizationDenied):
        async with database.transaction(context) as transaction:
            await transaction.get_user(user.id)
    assert client.database.collections.keys() == {"app_settings"}

    read_only = InternalAuthorizationContext(
        actor_id=uuid4(),
        capabilities=frozenset({StorageCapability.READ_IDENTITIES}),
    )
    with pytest.raises(AuthorizationDenied):
        async with database.transaction(read_only) as transaction:
            await transaction.create_user(user)
    assert client.database.collections.keys() == {"app_settings"}
    assert not hasattr(database, "client")
    assert not hasattr(database, "database")
    assert not hasattr(database, "get_collection")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collision",
    ["provider_account_id", "email_key"],
)
async def test_unique_user_collisions_return_safe_conflict(collision):
    database, client, events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    first = user_record()

    async def insert_first(transaction):
        await transaction.create_user(first)

    await fenced_write(database, actor_id, first.id, insert_first)
    first_policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    duplicate = user_record(
        id=uuid4(),
        provider_account_id=(
            first.provider_account_id
            if collision == "provider_account_id"
            else "other-subject"
        ),
        email_key=(
            first.email_key if collision == "email_key" else "another@example.com"
        ),
    )

    async def insert_duplicate(transaction):
        await transaction.create_user(duplicate)

    with pytest.raises(AppWriteConflict, match="already exists"):
        await fenced_write(
            database,
            actor_id,
            duplicate.id,
            insert_duplicate,
            policy_version=first_policy_version,
        )
    assert str(duplicate.id) not in client.database["users"].documents
    assert (
        client.database["app_settings"].documents["access_control"]["policy_version"]
        == first_policy_version
    )
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_role_compare_and_swap_versions_and_policy_once_per_transaction():
    database, client, _events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    role = role_record()
    user = user_record()

    async def insert_two_identities(transaction):
        await transaction.create_role(role)
        await transaction.create_user(user)

    await fenced_write(database, actor_id, role.id, insert_two_identities)
    settings = client.database["app_settings"].documents["access_control"]
    assert settings["policy_version"] == POLICY_VERSION + 1
    policy_increments = [
        write
        for write in client.database["app_settings"].writes
        if write[2] == {"$inc": {"policy_version": 1}}
    ]
    assert len(policy_increments) == 1

    changed = RoleRecord(
        **(
            role.model_dump()
            | {
                "version": 1,
                "active": True,
                "updated_at": NOW + timedelta(minutes=1),
            }
        )
    )
    async with database.transaction(full_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION + 1)
        await transaction.update_role(changed, expected_version=0)
        await transaction.append_required_audit(audit(actor_id, role.id))

    replace = client.database["roles"].writes[-1]
    assert replace[0] == "replace"
    assert replace[1] == {
        "_id": str(role.id),
        "version": 0,
        "created_at": NOW,
    }
    assert replace[2]["version"] == 1
    assert client.database["roles"].documents[str(role.id)]["active"] is True
    assert settings["policy_version"] == POLICY_VERSION + 2


@pytest.mark.asyncio
async def test_stale_role_version_aborts_without_policy_increment():
    database, client, events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    role = role_record()

    async def insert_role(transaction):
        await transaction.create_role(role)

    await fenced_write(database, actor_id, role.id, insert_role)
    policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    client.database["roles"].documents[str(role.id)]["version"] = 5
    changed = RoleRecord(
        **(role.model_dump() | {"version": 1, "updated_at": NOW + timedelta(minutes=1)})
    )
    with pytest.raises(AppWriteConflict, match="version changed"):
        async with database.transaction(full_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(policy_version)
            await transaction.update_role(changed, expected_version=0)
            await transaction.append_required_audit(audit(actor_id, role.id))
    assert (
        client.database["app_settings"].documents["access_control"]["policy_version"]
        == policy_version
    )
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_role_name_collision_returns_safe_conflict():
    database, client, events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    first = role_record()

    async def insert_first(transaction):
        await transaction.create_role(first)

    await fenced_write(database, actor_id, first.id, insert_first)
    policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    duplicate = role_record(id=uuid4(), name_key=first.name_key)

    async def insert_duplicate(transaction):
        await transaction.create_role(duplicate)

    with pytest.raises(AppWriteConflict, match="already exists"):
        await fenced_write(
            database,
            actor_id,
            duplicate.id,
            insert_duplicate,
            policy_version=policy_version,
        )
    assert str(duplicate.id) not in client.database["roles"].documents
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_user_profile_update_uses_cas_and_rejects_stale_version():
    database, client, events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    user = user_record()

    async def insert_user(transaction):
        await transaction.create_user(user)

    await fenced_write(database, actor_id, user.id, insert_user)
    policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    changed = UserRecord(
        **(
            user.model_dump()
            | {
                "display_name": "Updated Staff Member",
                "version": 1,
                "updated_at": NOW + timedelta(minutes=1),
            }
        )
    )
    async with database.transaction(full_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(policy_version)
        await transaction.update_user(changed, expected_version=0)
        await transaction.append_required_audit(audit(actor_id, user.id))

    replacement = client.database["users"].writes[-1]
    assert replacement[0] == "replace"
    assert replacement[1]["version"] == 0
    assert replacement[2]["display_name"] == "Updated Staff Member"
    assert replacement[2]["version"] == 1
    assert (
        client.database["app_settings"].documents["access_control"]["policy_version"]
        == policy_version + 1
    )

    stale = UserRecord(
        **(
            changed.model_dump()
            | {
                "display_name": "Stale Profile",
                "updated_at": NOW + timedelta(minutes=2),
            }
        )
    )
    next_policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    with pytest.raises(AppWriteConflict, match="version changed"):
        async with database.transaction(full_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(next_policy_version)
            await transaction.update_user(stale, expected_version=0)
            await transaction.append_required_audit(audit(actor_id, user.id))
    assert client.database["users"].documents[str(user.id)]["display_name"] == (
        "Updated Staff Member"
    )
    assert (
        client.database["app_settings"].documents["access_control"]["policy_version"]
        == next_policy_version
    )
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_email_collision_and_stable_provider_binding_are_not_merged():
    database, client, _events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    first = user_record()

    async def insert_first(transaction):
        await transaction.create_user(first)

    await fenced_write(database, actor_id, first.id, insert_first)
    policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    colliding_email = user_record(
        id=uuid4(),
        provider_account_id="different-google-subject",
        email_key=first.email_key.upper(),
    )

    async def insert_collision(transaction):
        await transaction.create_user(colliding_email)

    with pytest.raises(AppWriteConflict, match="already exists"):
        await fenced_write(
            database,
            actor_id,
            colliding_email.id,
            insert_collision,
            policy_version=policy_version,
        )

    changed_binding = UserRecord(
        **(
            first.model_dump()
            | {
                "provider_account_id": "replacement-google-subject",
                "version": 1,
                "updated_at": NOW + timedelta(minutes=1),
            }
        )
    )
    with pytest.raises(ValueError, match="binding and access cannot change"):
        async with database.transaction(full_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(policy_version)
            await transaction.update_user(changed_binding, expected_version=0)
    assert (
        client.database["users"].documents[str(first.id)]["provider_account_id"]
        == first.provider_account_id
    )


@pytest.mark.asyncio
async def test_policy_version_increment_conflict_poisons_identity_write():
    database, client, events = await connect_fake()
    await database.initialize_identity_indexes()
    actor_id = uuid4()
    user = user_record()
    client.database["app_settings"].reject_policy_increment = True

    with pytest.raises(AppWriteConflict, match="state is unavailable"):
        async with database.transaction(full_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.create_user(user)
            await transaction.append_required_audit(audit(actor_id, user.id))
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


def test_identity_models_enforce_bounded_typed_scope_and_document_size():
    with pytest.raises(ValidationError):
        Grant(
            dataset="byd-panel",
            sites=("site_a",),
            record_mode="selected",
            selected_records=(),
            fields=("site",),
        )
    with pytest.raises(ValidationError):
        Grant(
            dataset="byd-panel",
            sites=(),
            record_mode="assigned",
            fields=("site",),
        )
    with pytest.raises(ValidationError):
        SourceReference(
            database="byd-panel",
            collection="customers",
            key_field="_id",
            key_type="objectid",
            key_value="not-an-object-id",
        )
    with pytest.raises(ValidationError):
        UserRecord(**(user_record().model_dump() | {"provider_account_id": "x" * 256}))
    with pytest.raises(ValidationError):
        user_record(extra_source_password="must not be stored")
    with pytest.raises(ValidationError):
        RoleRecord(
            id=uuid4(),
            name_key="x" * 101,
            created_at=NOW,
            updated_at=NOW,
        )

    many_fields = tuple(f"f{index:02d}" + "x" * 97 for index in range(100))
    large_grant = Grant(
        dataset="byd-panel",
        sites=("site_a",),
        record_mode="assigned",
        fields=many_fields,
        metrics=many_fields,
    )
    large_role = role_record(grants=(large_grant,) * 100)
    with pytest.raises(ValueError, match="size limit"):
        large_role.document()


@pytest.mark.asyncio
async def test_oversized_stored_identity_is_rejected_without_returning_record():
    database, client, events = await connect_fake()
    user = user_record()
    document = user.document()
    document["display_name"] = "x" * (1_048_576 + 1)
    client.database["users"].documents[str(user.id)] = document
    reader = InternalAuthorizationContext(
        actor_id=uuid4(),
        capabilities=frozenset({StorageCapability.READ_IDENTITIES}),
    )

    with pytest.raises(AppDatabaseUnavailable, match="Identity record is unavailable"):
        async with database.transaction(reader) as transaction:
            await transaction.get_user(user.id)
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]
