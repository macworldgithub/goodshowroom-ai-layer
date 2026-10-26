"""TICKET-002 policy loading contracts with local Mongo fakes only."""

import inspect
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.read_concern import ReadConcern

from goodshowroom_ai.authorization import PolicySnapshot, Principal
from goodshowroom_ai.storage.identities import (
    Grant,
    RoleRecord,
    SourceReference,
    UserRecord,
)
from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.mongo import (
    APP_DATABASE_NAME,
    AppDatabase,
    AppDatabaseUnavailable,
    AuthorizationDenied,
)
from goodshowroom_ai.storage.settings import (
    AppSetting,
    IdentityMapping,
    SiteConfiguration,
)

NOW = datetime(2026, 10, 23, 9, tzinfo=UTC)
CREATED = NOW - timedelta(minutes=2)
UPDATED = NOW - timedelta(minutes=1)


def policy_records(*, mapping_count=0):
    user_id, role_id, login_id = uuid4(), uuid4(), uuid4()
    grant = Grant(
        dataset="byd-panel",
        sites=("site_a",),
        record_mode="all_permitted",
        fields=("site", "customer_id"),
        metrics=("sales_count",),
        tools=("customer_lookup",),
    )
    user = UserRecord(
        id=user_id,
        provider_account_id="google-subject-1",
        email_key="staff@example.com",
        approval_status="approved",
        role_id=role_id,
        scope_limit=(grant,),
        created_at=CREATED,
        updated_at=UPDATED,
    )
    role = RoleRecord(
        id=role_id,
        name_key="sales_viewer",
        active=True,
        grants=(grant,),
        capabilities=frozenset({"chat"}),
        created_at=CREATED,
        updated_at=UPDATED,
    )
    login = LoginRecord(
        id=login_id,
        user_id=user_id,
        generation=4,
        auth_started_at=CREATED,
        created_at=CREATED,
        updated_at=UPDATED,
        auth_expires_at=NOW + timedelta(hours=9),
        expires_at=NOW + timedelta(hours=7),
        lease_valid_until=NOW + timedelta(minutes=4),
    )
    mappings = []
    for number in range(mapping_count):
        verified_at = UPDATED
        mappings.append(
            IdentityMapping(
                id=uuid4(),
                user_id=user_id,
                source_ref=SourceReference(
                    database="byd-panel",
                    collection="users",
                    key_field="_id",
                    key_type="objectid",
                    key_value=f"{number + 1:024x}",
                ),
                active=True,
                sites=("site_a",),
                verified_by=uuid4(),
                verified_at=verified_at,
                created_at=verified_at - timedelta(seconds=1),
                updated_at=verified_at,
            )
        )
    sites = AppSetting(
        id="sites",
        sites=(SiteConfiguration(site_id="site_a"),),
        created_at=CREATED,
        updated_at=UPDATED,
    )
    return user, role, login, tuple(mappings), sites


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents
        self.limit_value = None

    def limit(self, value):
        self.limit_value = value
        return self

    async def to_list(self, *, length):
        size = min(length, self.limit_value or length)
        return deepcopy(self.documents[:size])


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.documents = {}
        self.find_one_calls = []
        self.find_calls = []
        self.writes = []

    async def find_one(self, query, *, session):
        assert session is not None
        self.find_one_calls.append((deepcopy(query), session))
        document = self.documents.get(query.get("_id"))
        if document is None or any(
            document.get(key) != value for key, value in query.items()
        ):
            return None
        return deepcopy(document)

    def find(self, query, *, projection, session):
        assert session is not None
        self.find_calls.append((deepcopy(query), deepcopy(projection), session))
        matches = [
            document
            for document in self.documents.values()
            if all(document.get(key) == value for key, value in query.items())
        ]
        return FakeCursor(matches)


class FakeDatabase:
    def __init__(self, *, mapping_count=0, policy_version=23):
        user, role, login, mappings, sites = policy_records(mapping_count=mapping_count)
        self.collections = {
            "app_settings": FakeCollection("app_settings"),
            "users": FakeCollection("users"),
            "roles": FakeCollection("roles"),
            "login_sessions": FakeCollection("login_sessions"),
            "identity_mappings": FakeCollection("identity_mappings"),
        }
        self.collections["app_settings"].documents.update(
            {
                "access_control": {
                    "_id": "access_control",
                    "policy_version": policy_version,
                    "commit_fence": 0,
                },
                sites.id: sites.document(),
            }
        )
        self.collections["users"].documents[str(user.id)] = user.document()
        self.collections["roles"].documents[str(role.id)] = role.document()
        self.collections["login_sessions"].documents[str(login.id)] = login.document()
        self.collections["identity_mappings"].documents.update(
            {str(mapping.id): mapping.document() for mapping in mappings}
        )
        self.records = (user, role, login, mappings, sites)

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(name))


class FakeTransaction:
    def __init__(self, session, options):
        self.session = session
        self.options = options

    async def __aenter__(self):
        self.session.events.append("transaction-start")
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        self.session.events.append(
            "transaction-abort" if exc_type else "transaction-commit"
        )
        return False


class FakeSession:
    def __init__(self):
        self.events = []
        self.options = None

    def bind(self):
        return FakeSessionContext(self)

    async def start_transaction(self, **options):
        self.options = options
        return FakeTransaction(self, options)


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
    def __init__(self):
        self.session = FakeSession()
        self.database = FakeDatabase()
        self.selected_databases = [APP_DATABASE_NAME]

    def start_session(self):
        return self.session


def connected_fake(*, mapping_count=0, policy_version=23):
    client = FakeClient()
    if mapping_count != 0 or policy_version != 23:
        client.database = FakeDatabase(
            mapping_count=mapping_count,
            policy_version=policy_version,
        )
    database = AppDatabase(client, client.database)
    user, _role, login, _mappings, _sites = client.database.records
    principal = Principal(
        user_id=user.id, login_id=login.id, generation=login.generation
    )
    return database, client, principal


def assert_no_writes(database):
    assert all(not collection.writes for collection in database.collections.values())


@pytest.mark.asyncio
async def test_policy_loader_reads_one_typed_snapshot_with_snapshot_concern():
    database, client, principal = connected_fake(mapping_count=2)

    policy = await database.load_authorization_policy(principal)

    assert isinstance(policy, PolicySnapshot)
    assert policy.policy_version == 23
    assert policy.user.id == principal.user_id
    assert policy.login.id == principal.login_id
    assert policy.role.active is True
    assert policy.sites is not None
    assert policy.sites.id == "sites"
    assert policy.mappings == tuple(
        sorted(policy.mappings, key=lambda item: str(item.id))
    )
    assert len(policy.mappings) == 2
    assert client.session.options["read_concern"] == ReadConcern("snapshot")
    assert client.session.events == [
        "session-start",
        "transaction-start",
        "transaction-commit",
        "session-close",
    ]
    assert set(client.database.collections) == {
        "app_settings",
        "users",
        "roles",
        "login_sessions",
        "identity_mappings",
    }
    assert all(
        call[0] == {"user_id": str(principal.user_id), "active": True}
        for call in client.database["identity_mappings"].find_calls
    )
    assert_no_writes(client.database)


@pytest.mark.asyncio
async def test_policy_loader_rejects_an_untyped_principal_before_opening_session():
    database, client, _principal = connected_fake()

    with pytest.raises(AuthorizationDenied):
        await database.load_authorization_policy(str(uuid4()))

    assert client.session.events == []
    assert_no_writes(client.database)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_version", [None, -1, True, 1.5])
async def test_policy_loader_denies_missing_or_malformed_policy_version(policy_version):
    database, client, principal = connected_fake()
    setting = client.database["app_settings"].documents["access_control"]
    if policy_version is None:
        del client.database["app_settings"].documents["access_control"]
    else:
        setting["policy_version"] = policy_version

    with pytest.raises(AppDatabaseUnavailable, match="Policy is unavailable"):
        await database.load_authorization_policy(principal)

    assert client.session.events[-1] == "session-close"
    assert_no_writes(client.database)


@pytest.mark.asyncio
async def test_policy_loader_rejects_more_active_mappings_than_its_bound():
    database, client, principal = connected_fake(mapping_count=101)

    with pytest.raises(AppDatabaseUnavailable, match="Policy is unavailable"):
        await database.load_authorization_policy(principal)

    mapping_collection = client.database["identity_mappings"]
    assert mapping_collection.find_calls[0][0] == {
        "user_id": str(principal.user_id),
        "active": True,
    }
    assert_no_writes(client.database)


@pytest.mark.asyncio
async def test_policy_loader_fails_closed_when_user_role_or_login_is_missing():
    for collection_name in ("users", "roles", "login_sessions"):
        database, client, principal = connected_fake()
        collection = client.database[collection_name]
        if collection_name == "roles":
            role_id = client.database.records[0].role_id
            collection.documents.pop(str(role_id))
        elif collection_name == "users":
            collection.documents.pop(str(principal.user_id))
        else:
            collection.documents.pop(str(principal.login_id))

        with pytest.raises(AuthorizationDenied):
            await database.load_authorization_policy(principal)
        assert_no_writes(client.database)


def test_installed_pymongo_transaction_start_is_async():
    assert inspect.iscoroutinefunction(AsyncClientSession.start_transaction)
