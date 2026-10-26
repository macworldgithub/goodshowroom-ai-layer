"""TICKET-003 operational read boundary tests using synthetic policy and data."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bson import Int64, ObjectId
from pydantic import SecretStr, ValidationError
from pymongo.collation import Collation

import goodshowroom_ai.operational as operational
from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    CollectionPolicy,
    PolicySnapshot,
    Principal,
)
from goodshowroom_ai.operational import (
    OperationalReadRepository,
    SourceUnavailable,
    _validate_pipeline,
    connect_operational_repository,
)
from goodshowroom_ai.operational_models import (
    CollectionBinding,
    DateFilter,
    EqualFilter,
    FieldBinding,
    JoinBinding,
    ReadRequest,
    SiteBinding,
    StaffBinding,
)
from goodshowroom_ai.storage.identities import (
    Grant,
    RoleRecord,
    SourceReference,
    UserRecord,
)
from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.settings import (
    AppSetting,
    IdentityMapping,
    SiteConfiguration,
)

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)
LEAD_ID = "lead-42"
STAFF_ID = "a" * 24
JOINED_LEAD_ID = "lead-42"
MONGO_ID_1 = ObjectId("000000000000000000000001")
MONGO_ID_2 = ObjectId("000000000000000000000002")
MONGO_ID_3 = ObjectId("000000000000000000000003")


def reference(
    collection="leads",
    *,
    database="byd-leads-new",
    key_field="leadId",
    key_type="string",
    key_value=LEAD_ID,
):
    return SourceReference(
        database=database,
        collection=collection,
        key_field=key_field,
        key_type=key_type,
        key_value=key_value,
    )


def lead_binding():
    return CollectionBinding(
        name="leads",
        database="byd-leads-new",
        collection="leads",
        fields=(
            FieldBinding(name="lead_id", path="leadId", kind="string"),
            FieldBinding(name="site", path="dealership", kind="string"),
            FieldBinding(name="assigned_staff", path="assignedUserId", kind="objectid"),
            FieldBinding(name="status", path="status", kind="string"),
            FieldBinding(name="created_at", path="createdAt", kind="date"),
            FieldBinding(name="join_id", path="externalId", kind="string"),
            FieldBinding(name="amount", path="score", kind="number"),
            FieldBinding(name="private_note", path="privateNote", kind="string"),
        ),
        identity="lead_id",
        site_field="site",
        sites=(
            SiteBinding(site="site_a", values=("dealer-code-a", "showroom-a")),
            SiteBinding(site="site_b", values=("dealer-code-b",)),
        ),
        staff=(
            StaffBinding(
                field="assigned_staff",
                database="byd-leads-new",
                collection="users",
                key_field="_id",
                key_type="objectid",
            ),
        ),
    )


def delivery_binding():
    return CollectionBinding(
        name="deliveries",
        database="byd-leads-new",
        collection="deliveries",
        fields=(
            FieldBinding(name="delivery_id", path="deliveryId", kind="string"),
            FieldBinding(name="site", path="dealership", kind="string"),
            FieldBinding(name="assigned_staff", path="assignedUserId", kind="objectid"),
            FieldBinding(name="lead_id", path="leadId", kind="string"),
            FieldBinding(name="status", path="status", kind="string"),
            FieldBinding(name="private_note", path="privateNote", kind="string"),
        ),
        identity="delivery_id",
        site_field="site",
        sites=(SiteBinding(site="site_a", values=("dealer-code-a",)),),
        staff=(
            StaffBinding(
                field="assigned_staff",
                database="byd-leads-new",
                collection="users",
                key_field="_id",
                key_type="objectid",
            ),
        ),
    )


LEADS = lead_binding()
DELIVERIES = delivery_binding()
JOIN = JoinBinding(
    name="lead_deliveries",
    left="leads",
    right="deliveries",
    left_field="lead_id",
    right_field="lead_id",
)


def default_document(*, mongo_id=MONGO_ID_1, lead_id=LEAD_ID, site="dealer-code-a"):
    return {
        "_id": mongo_id,
        "leadId": lead_id,
        "dealership": site,
        "assignedUserId": ObjectId(STAFF_ID),
        "status": "open",
        "createdAt": NOW - timedelta(days=1),
        "externalId": "external-42",
        "score": 1.5,
        "privateNote": "synthetic note",
    }


def delivery_document(*, site="dealer-code-a", lead_id=JOINED_LEAD_ID):
    return {
        "_id": MONGO_ID_1,
        "deliveryId": "delivery-7",
        "dealership": site,
        "assignedUserId": ObjectId(STAFF_ID),
        "leadId": lead_id,
        "status": "delivered",
        "privateNote": "synthetic delivery note",
    }


class FakeCursor:
    def __init__(self, documents=None, *, list_error=None):
        self.documents = [] if documents is None else documents
        self.list_error = list_error
        self.closed = False
        self.requested_length = None
        self.requested_lengths = []
        self.to_list_calls = 0
        self.started = asyncio.Event()
        self.never = asyncio.Event()

    async def to_list(self, *, length):
        self.requested_length = length
        self.requested_lengths.append(length)
        self.to_list_calls += 1
        self.started.set()
        if self.list_error is not None:
            raise self.list_error
        if self.documents == "wait":
            await self.never.wait()
        batch = self.documents[:length]
        self.documents = self.documents[length:]
        return batch

    async def close(self):
        self.closed = True


class FakeCollection:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.write_calls = []
        self.aggregate_error = None
        self.on_aggregate = None
        self.cursors = []

    async def aggregate(self, pipeline, **kwargs):
        self.calls.append((deepcopy(pipeline), kwargs))
        if self.on_aggregate is not None:
            self.on_aggregate()
        if self.aggregate_error is not None:
            raise self.aggregate_error
        response = self.responses.pop(0) if self.responses else []
        if isinstance(response, FakeCursor):
            cursor = response
        elif isinstance(response, Exception):
            raise response
        else:
            cursor = FakeCursor(response)
        self.cursors.append(cursor)
        return cursor

    async def insert_one(self, *_args, **_kwargs):
        self.write_calls.append("insert_one")

    async def insert_many(self, *_args, **_kwargs):
        self.write_calls.append("insert_many")

    async def update_one(self, *_args, **_kwargs):
        self.write_calls.append("update_one")

    async def update_many(self, *_args, **_kwargs):
        self.write_calls.append("update_many")

    async def replace_one(self, *_args, **_kwargs):
        self.write_calls.append("replace_one")

    async def delete_one(self, *_args, **_kwargs):
        self.write_calls.append("delete_one")

    async def delete_many(self, *_args, **_kwargs):
        self.write_calls.append("delete_many")

    async def find_one_and_update(self, *_args, **_kwargs):
        self.write_calls.append("find_one_and_update")


class FakeDatabase:
    def __init__(self, name):
        self.name = name
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())

    def with_options(self, **_kwargs):
        return self


class FakeClient:
    def __init__(self):
        self.database_names = []
        self.databases = {}
        self.close_calls = 0

    def __getitem__(self, name):
        self.database_names.append(name)
        return self.databases.setdefault(name, FakeDatabase(name))

    async def close(self):
        self.close_calls += 1


def policy_fixture(
    bindings=(LEADS,),
    *,
    record_mode="all_permitted",
    selected_records=(),
    grant_sites=("site_a",),
    available_sites=("site_a", "site_b"),
    allowed_fields=None,
    allowed_tools=None,
    allowed_metrics=("lead_count",),
    mappings=(),
    policy_version=7,
    loader=None,
):
    user_id, login_id, role_id = uuid4(), uuid4(), uuid4()
    principal = Principal(user_id=user_id, login_id=login_id, generation=3)
    created = NOW - timedelta(minutes=3)
    updated = NOW - timedelta(minutes=1)
    sites = AppSetting(
        id="sites",
        sites=tuple(SiteConfiguration(site_id=site) for site in available_sites),
        created_at=created,
        updated_at=updated,
    )
    grant_fields = allowed_fields or {
        binding.name: tuple(field.name for field in binding.fields)
        for binding in bindings
    }
    grant_tools = allowed_tools or {
        binding.name: ("lead_list", "delivery_list") for binding in bindings
    }
    grants = tuple(
        Grant(
            dataset=binding.database,
            sites=tuple(grant_sites),
            record_mode=record_mode,
            selected_records=tuple(selected_records),
            fields=tuple(grant_fields[binding.name]),
            metrics=tuple(allowed_metrics),
            tools=tuple(grant_tools[binding.name]),
        )
        for binding in bindings
    )
    user = UserRecord(
        id=user_id,
        provider_account_id="synthetic-provider-id",
        email_key="synthetic@example.invalid",
        approval_status="approved",
        role_id=role_id,
        scope_limit=grants,
        created_at=created,
        updated_at=updated,
    )
    role = RoleRecord(
        id=role_id,
        name_key="synthetic_viewer",
        active=True,
        grants=grants,
        capabilities=frozenset({"chat"}),
        created_at=created,
        updated_at=updated,
    )
    login = LoginRecord(
        id=login_id,
        user_id=user_id,
        generation=3,
        auth_started_at=NOW - timedelta(minutes=4),
        created_at=created,
        updated_at=updated,
        auth_expires_at=NOW + timedelta(hours=7),
        expires_at=NOW + timedelta(hours=6),
        lease_valid_until=NOW + timedelta(minutes=3),
    )
    snapshot = PolicySnapshot(
        policy_version=policy_version,
        user=user,
        role=role,
        login=login,
        mappings=tuple(mappings),
        sites=sites,
    )
    state = {"snapshot": snapshot, "identity_ok": True, "load_count": 0}

    async def load_policy(_principal):
        state["load_count"] += 1
        if loader is not None:
            return loader(state, _principal)
        return state["snapshot"]

    async def check_identity(_principal):
        return state["identity_ok"]

    collections = tuple(
        CollectionPolicy(
            database=binding.database,
            collection=binding.collection,
            fields=frozenset(
                allowed_fields[binding.name]
                if allowed_fields is not None and binding.name in allowed_fields
                else (field.name for field in binding.fields)
            ),
            tools=frozenset(
                grant_tools[binding.name]
                if binding.name in grant_tools
                else ("lead_list", "delivery_list")
            ),
            metrics=frozenset(allowed_metrics),
        )
        for binding in bindings
    )
    authorization = AuthorizationService(
        load_policy=load_policy,
        check_identity=check_identity,
        clock=lambda: NOW,
        collections=collections,
    )
    return {
        "principal": principal,
        "snapshot": snapshot,
        "state": state,
        "authorization": authorization,
    }


def request(
    *,
    binding="leads",
    site="site_a",
    tool="lead_list",
    fields=frozenset({"status"}),
    filters=(),
    dates=None,
    metric=None,
    page_size=25,
):
    return ReadRequest(
        binding=binding,
        site=site,
        tool=tool,
        fields=fields,
        filters=tuple(filters),
        dates=dates,
        metric=metric,
        page_size=page_size,
    )


def repository(fixture, *, bindings=(LEADS,), joins=()):
    database = FakeDatabase(bindings[0].database)
    repo = OperationalReadRepository(
        databases={bindings[0].database: database},
        authorization=fixture["authorization"],
        bindings=bindings,
        joins=joins,
    )
    return repo, database


def all_subdocuments(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from all_subdocuments(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from all_subdocuments(child)


@pytest.mark.asyncio
async def test_public_interface_has_only_named_reads_and_never_calls_write_commands():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    collection = database["leads"]
    collection.responses = [[default_document()]]

    public_methods = {name for name in dir(repo) if not name.startswith("_")}
    assert public_methods == {
        "close",
        "count_date_cohort",
        "count_records",
        "list_joined_records",
        "list_records",
    }
    assert not any(
        hasattr(repo, name)
        for name in (
            "client",
            "database",
            "collection",
            "execute",
            "aggregate",
            "command",
            "insert_one",
            "update_one",
            "replace_one",
            "delete_one",
        )
    )

    await repo.list_records(fixture["principal"], request())

    assert len(collection.calls) == 1
    assert collection.write_calls == []


@pytest.mark.asyncio
async def test_no_binding_defaults_or_app_database_are_available():
    fixture = policy_fixture()
    db = FakeDatabase("byd-leads-new")
    unconfigured = OperationalReadRepository(
        databases={"byd-leads-new": db}, authorization=fixture["authorization"]
    )
    with pytest.raises(SourceUnavailable):
        await unconfigured.list_records(fixture["principal"], request())
    assert db.collections == {}

    with pytest.raises(ValueError):
        OperationalReadRepository(
            databases={"byd_ai_layer": FakeDatabase("byd_ai_layer")},
            authorization=fixture["authorization"],
        )

    with pytest.raises(ValueError):
        OperationalReadRepository(
            databases={"byd-leads-new": FakeDatabase("byd-panel")},
            authorization=fixture["authorization"],
        )


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$match": {"status": {"$where": "return true"}}}],
        [{"$match": {"$or": [{"status": {"$regex": ".*"}}]}}],
        [{"$unionWith": "users"}],
        [{"$match": {"$expr": {"$eq": [{"$function": {"body": "x"}}, 1]}}}],
        [{"$match": {"status": "open"}}, {"$out": "leads_copy"}],
        [{"$match": {"status": "open"}}, {"$merge": "leads_copy"}],
        [{"$project": {"passwordHash": 1}}],
        [{"$limit": 0}],
    ],
    ids=(
        "where",
        "regex",
        "union",
        "function",
        "out",
        "merge",
        "unknown-field",
        "zero-limit",
    ),
)
def test_private_pipeline_validator_rejects_operators_stages_and_unapproved_fields(
    pipeline,
):
    with pytest.raises(ValueError):
        _validate_pipeline(pipeline, LEADS, (DELIVERIES,))


@pytest.mark.parametrize(
    "nested_stage",
    [
        {"$out": "leads_copy"},
        {"$merge": "leads_copy"},
        {"$match": {"status": {"$where": "return true"}}},
        {"$match": {"$expr": {"$eq": [{"$accumulator": {"init": "x"}}, 1]}}},
        {"$match": {"$expr": {"$eq": [{"$function": {"body": "x"}}, 1]}}},
        {"$lookup": {"from": "users", "pipeline": [], "as": "escape"}},
    ],
    ids=(
        "nested-out",
        "nested-merge",
        "nested-where",
        "nested-accumulator",
        "nested-function",
        "nested-lookup",
    ),
)
def test_private_pipeline_validator_recursively_rejects_nested_escapes(nested_stage):
    pipeline = [
        {
            "$lookup": {
                "from": "deliveries",
                "let": {"join_value": "$leadId"},
                "pipeline": [{"$match": {"status": "delivered"}}, nested_stage],
                "as": "_joined",
            }
        }
    ]
    with pytest.raises(ValueError):
        _validate_pipeline(pipeline, LEADS, (DELIVERIES,))


def test_validator_accepts_only_the_explicitly_authorized_lookup_target():
    lookup = {
        "$lookup": {
            "from": "deliveries",
            "let": {"join_value": "$leadId"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$leadId", "$$join_value"]}}},
                {"$limit": 1},
                {"$project": {"_id": 1, "deliveryId": 1, "leadId": 1}},
            ],
            "as": "_joined",
        }
    }

    with pytest.raises(ValueError):
        _validate_pipeline([lookup], LEADS, ())
    _validate_pipeline([lookup], LEADS, (DELIVERIES,))

    unapproved = deepcopy(lookup)
    unapproved["$lookup"]["from"] = "users"
    with pytest.raises(ValueError):
        _validate_pipeline([unapproved], LEADS, (DELIVERIES,))


@pytest.mark.asyncio
async def test_trusted_read_pipeline_applies_site_scope_projection_page_limit_and_deadline():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    collection = database["leads"]
    collection.responses = [[default_document()]]

    page = await repo.list_records(fixture["principal"], request(page_size=4))

    pipeline, options = collection.calls[0]
    assert [next(iter(stage)) for stage in pipeline] == [
        "$match",
        "$sort",
        "$limit",
        "$project",
    ]
    assert pipeline[1] == {"$sort": {"_id": 1}}
    assert pipeline[2] == {"$limit": 5}
    projection = pipeline[3]["$project"]
    assert set(projection) == {
        "_id",
        "leadId",
        "dealership",
        "assignedUserId",
        "status",
    }
    assert options["maxTimeMS"] == 10_000
    assert options["allowDiskUse"] is False
    assert options["batchSize"] == 1
    assert options["collation"] == Collation(locale="simple")
    assert collection.cursors[0].requested_lengths
    assert set(collection.cursors[0].requested_lengths) == {1}
    assert collection.cursors[0].closed
    assert page.rows[0].values == {"status": "open"}
    assert "private_note" not in page.rows[0].values
    scope = pipeline[0]["$match"]["$and"][0]
    assert {"dealership": {"$in": ["dealer-code-a", "showroom-a"]}} in all_subdocuments(
        scope
    )
    assert {"$expr": {"$eq": [{"$type": "$leadId"}, "string"]}} in all_subdocuments(
        scope
    )
    assert {"$expr": {"$eq": [{"$type": "$dealership"}, "string"]}} in all_subdocuments(
        scope
    )


@pytest.mark.asyncio
async def test_selected_records_are_source_qualified_and_filtered_before_execution():
    selected = reference(key_value="lead-selected")
    fixture = policy_fixture(record_mode="selected", selected_records=(selected,))
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [[default_document(lead_id="lead-selected")]]

    await repo.list_records(fixture["principal"], request())

    scope = database["leads"].calls[0][0][0]["$match"]["$and"][0]
    assert {"leadId": {"$in": ["lead-selected"]}} in all_subdocuments(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected",
    [
        reference(collection="appointments"),
        reference(key_field="anotherId"),
    ],
    ids=("wrong-source-collection", "wrong-source-key"),
)
async def test_selected_reference_with_wrong_source_identity_never_reaches_driver(
    selected,
):
    fixture = policy_fixture(record_mode="selected", selected_records=(selected,))
    repo, database = repository(fixture, bindings=(LEADS,))

    with pytest.raises((AccessDenied, SourceUnavailable)):
        await repo.list_records(fixture["principal"], request())

    assert database.collections == {}


@pytest.mark.asyncio
async def test_assigned_records_use_verified_source_qualified_staff_mapping():
    staff_reference = reference(
        collection="users",
        key_field="_id",
        key_type="objectid",
        key_value=STAFF_ID,
    )
    created = NOW - timedelta(minutes=4)
    mapping = IdentityMapping(
        id=uuid4(),
        user_id=uuid4(),  # Replaced with the fixture user below after fixture creation.
        source_ref=staff_reference,
        active=True,
        sites=("site_a",),
        verified_by=uuid4(),
        verified_at=NOW - timedelta(minutes=2),
        created_at=created,
        updated_at=NOW - timedelta(minutes=1),
    )
    fixture = policy_fixture(record_mode="assigned")
    mapping = mapping.model_copy(update={"user_id": fixture["principal"].user_id})
    fixture["state"]["snapshot"] = fixture["snapshot"].model_copy(
        update={"mappings": (mapping,)}
    )
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [[default_document()]]

    await repo.list_records(fixture["principal"], request())

    scope = database["leads"].calls[0][0][0]["$match"]["$and"][0]
    assert {"assignedUserId": {"$in": [ObjectId(STAFF_ID)]}} in all_subdocuments(scope)
    assert {
        "$expr": {"$eq": [{"$type": "$assignedUserId"}, "objectId"]}
    } in all_subdocuments(scope)


@pytest.mark.asyncio
async def test_assigned_mapping_without_a_reviewed_staff_binding_fails_unavailable():
    fixture = policy_fixture(record_mode="assigned")
    staff_reference = reference(
        collection="staff_site_memberships",
        key_field="staff_id",
        key_type="objectid",
        key_value=STAFF_ID,
    )
    created = NOW - timedelta(minutes=4)
    mapping = IdentityMapping(
        id=uuid4(),
        user_id=fixture["principal"].user_id,
        source_ref=staff_reference,
        active=True,
        sites=("site_a",),
        verified_by=uuid4(),
        verified_at=NOW - timedelta(minutes=2),
        created_at=created,
        updated_at=NOW - timedelta(minutes=1),
    )
    fixture["state"]["snapshot"] = fixture["snapshot"].model_copy(
        update={"mappings": (mapping,)}
    )
    repo, database = repository(fixture, bindings=(LEADS,))

    with pytest.raises(SourceUnavailable):
        await repo.list_records(fixture["principal"], request())

    assert database.collections == {}


@pytest.mark.asyncio
async def test_unmapped_assigned_scope_is_denied_before_aggregate():
    fixture = policy_fixture(record_mode="assigned")
    repo, database = repository(fixture, bindings=(LEADS,))

    with pytest.raises(AccessDenied):
        await repo.list_records(fixture["principal"], request())

    assert database.collections == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_request",
    [
        request(fields=frozenset({"private_note"})),
        request(site="site_b"),
        request(fields=frozenset({"not_bound"})),
    ],
    ids=("ungranted-field", "ungranted-site", "unbound-field"),
)
async def test_unauthorized_or_unbound_fields_and_sites_are_denied_before_read(
    bad_request,
):
    allowed_fields = {LEADS.name: ("lead_id", "site", "assigned_staff", "status")}
    fixture = policy_fixture(allowed_fields=allowed_fields, grant_sites=("site_a",))
    repo, database = repository(fixture, bindings=(LEADS,))

    with pytest.raises((AccessDenied, SourceUnavailable)):
        await repo.list_records(fixture["principal"], bad_request)

    assert database.collections == {}


@pytest.mark.asyncio
async def test_malformed_untyped_and_hostile_filter_inputs_do_not_execute():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))

    with pytest.raises(SourceUnavailable):
        await repo.list_records(
            fixture["principal"], {"binding": "leads", "$where": "x"}
        )
    with pytest.raises(ValidationError):
        EqualFilter(field="status", value={"$gt": "closed"})
    hostile_field_filter = EqualFilter(field="$where", value="return true")
    with pytest.raises(SourceUnavailable):
        await repo.list_records(
            fixture["principal"], request(filters=(hostile_field_filter,))
        )
    with pytest.raises(ValidationError):
        request(page_size=101)
    with pytest.raises(ValidationError):
        DateFilter(
            field="created_at",
            start=NOW,
            end=NOW - timedelta(seconds=1),
        )
    with pytest.raises(SourceUnavailable):
        await repo.list_records(
            fixture["principal"],
            request(
                dates=DateFilter(field="status", start=NOW - timedelta(days=1), end=NOW)
            ),
        )

    assert database.collections == {}


@pytest.mark.asyncio
async def test_equality_and_half_open_date_filters_are_built_from_typed_fields():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [[default_document()]]
    start = NOW - timedelta(days=7)
    end = NOW

    await repo.list_records(
        fixture["principal"],
        request(
            filters=(EqualFilter(field="status", value="open"),),
            dates=DateFilter(field="created_at", start=start, end=end),
        ),
    )

    clauses = database["leads"].calls[0][0][0]["$match"]["$and"]
    assert {"status": {"$eq": "open"}} in clauses
    assert {"createdAt": {"$gte": start, "$lt": end}} in clauses


@pytest.mark.asyncio
async def test_count_uses_full_scoped_aggregation_with_no_page_limit_and_zero_is_exact():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    collection = database["leads"]
    collection.responses = [[], [{"count": 3}]]
    count_request = request(metric="lead_count", page_size=1)

    empty = await repo.count_records(fixture["principal"], count_request)
    assert empty.count == 0
    result = await repo.count_records(fixture["principal"], count_request)

    pipeline = collection.calls[0][0]
    assert pipeline[-1] == {"$count": "count"}
    assert {"dealership": {"$in": ["dealer-code-a", "showroom-a"]}} in all_subdocuments(
        pipeline[0]["$match"]
    )
    assert all("$limit" not in stage for stage in pipeline)
    assert result.count == 3
    assert collection.calls[1][0][-1] == {"$count": "count"}


@pytest.mark.asyncio
async def test_count_accepts_large_bson_int64_and_returns_a_python_int():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    large_count = 9_000_000_000
    database["leads"].responses = [[{"count": Int64(large_count)}]]

    result = await repo.count_records(
        fixture["principal"], request(metric="lead_count")
    )

    assert result.count == large_count
    assert type(result.count) is int


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        [{"count": True}],
        [{"count": -1}],
        [{"count": 1}, {"count": 2}],
        [{"unexpected": 1}],
    ],
    ids=("bool-is-not-int", "negative", "multiple-count-rows", "wrong-field"),
)
async def test_malformed_count_results_fail_closed(result):
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [result]

    with pytest.raises(SourceUnavailable, match="Source is unavailable"):
        await repo.count_records(fixture["principal"], request(metric="lead_count"))


@pytest.mark.asyncio
async def test_count_without_approved_metric_does_not_reach_database():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))

    with pytest.raises(AccessDenied):
        await repo.count_records(fixture["principal"], request())

    assert database.collections == {}


@pytest.mark.asyncio
async def test_source_failure_is_sanitized_and_cursor_is_closed():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    collection = database["leads"]
    cursor = FakeCursor(list_error=RuntimeError("private URI and source document"))
    collection.responses = [cursor]

    with pytest.raises(SourceUnavailable) as error:
        await repo.list_records(fixture["principal"], request())

    assert str(error.value) == "Source is unavailable."
    assert "private URI" not in str(error.value)
    assert cursor.closed


@pytest.mark.asyncio
async def test_bson_pipeline_and_source_result_size_caps_precede_release(monkeypatch):
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    collection = database["leads"]

    monkeypatch.setattr(operational, "MAX_APP_DOCUMENT_BYTES", 128)
    with pytest.raises(SourceUnavailable):
        await repo.list_records(fixture["principal"], request())
    assert collection.calls == []

    monkeypatch.setattr(operational, "MAX_APP_DOCUMENT_BYTES", 1000)
    cursor = FakeCursor([default_document() | {"status": "x" * 2000}])
    collection.responses = [cursor]
    with pytest.raises(SourceUnavailable):
        await repo.list_records(fixture["principal"], request())
    assert cursor.closed


@pytest.mark.asyncio
async def test_oversized_first_result_closes_cursor_without_draining_more(monkeypatch):
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    cursor = FakeCursor(
        [
            default_document() | {"status": "x" * 2000},
            default_document(mongo_id=MONGO_ID_2, lead_id="lead-2"),
        ]
    )
    database["leads"].responses = [cursor]
    monkeypatch.setattr(operational, "MAX_APP_DOCUMENT_BYTES", 1000)

    with pytest.raises(SourceUnavailable):
        await repo.list_records(fixture["principal"], request(page_size=10))

    assert cursor.closed
    assert cursor.to_list_calls == 1
    assert cursor.requested_lengths == [1]


@pytest.mark.asyncio
async def test_stable_id_pagination_binds_user_policy_request_and_page_size():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    collection = database["leads"]
    first = request(page_size=2)
    collection.responses = [
        [
            default_document(mongo_id=MONGO_ID_1, lead_id="lead-1"),
            default_document(mongo_id=MONGO_ID_2, lead_id="lead-2"),
            default_document(mongo_id=MONGO_ID_3, lead_id="lead-3"),
        ],
        [default_document(mongo_id=MONGO_ID_3, lead_id="lead-3")],
    ]

    first_page = await repo.list_records(fixture["principal"], first)
    next_page = await repo.list_records(
        fixture["principal"], first, position=first_page.next_position
    )

    assert [row.reference.key_value for row in first_page.rows] == ["lead-1", "lead-2"]
    assert first_page.next_position is not None
    assert first_page.next_position.after.key_field == "_id"
    assert first_page.next_position.after.key_value == str(MONGO_ID_2)
    assert next_page.next_position is None
    next_pipeline = collection.calls[1][0]
    assert next_pipeline[1] == {"$sort": {"_id": 1}}
    assert next_pipeline[2] == {"$limit": 3}
    assert {"_id": {"$gt": MONGO_ID_2}} in all_subdocuments(next_pipeline[0])

    collection.responses = [[]]
    foreign_user_position = first_page.next_position.model_copy(
        update={"user_id": uuid4()}
    )
    with pytest.raises(AccessDenied):
        await repo.list_records(
            fixture["principal"], first, position=foreign_user_position
        )
    assert len(collection.calls) == 2

    stale_position = first_page.next_position.model_copy(update={"policy_version": 6})
    with pytest.raises(AccessDenied):
        await repo.list_records(fixture["principal"], first, position=stale_position)
    assert len(collection.calls) == 2

    changed_filter_position = first_page.next_position
    with pytest.raises(AccessDenied):
        await repo.list_records(
            fixture["principal"],
            request(page_size=2, filters=(EqualFilter(field="status", value="open"),)),
            position=changed_filter_position,
        )
    assert len(collection.calls) == 2


@pytest.mark.asyncio
async def test_policy_version_change_invalidates_a_page_position_before_execution():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [
        [default_document(mongo_id=MONGO_ID_1), default_document(mongo_id=MONGO_ID_2)]
    ]
    page = await repo.list_records(fixture["principal"], request(page_size=1))
    snapshot = fixture["state"]["snapshot"]
    fixture["state"]["snapshot"] = snapshot.model_copy(update={"policy_version": 8})

    with pytest.raises(AccessDenied):
        await repo.list_records(
            fixture["principal"], request(page_size=1), position=page.next_position
        )

    assert len(database["leads"].calls) == 1


@pytest.mark.asyncio
async def test_join_requires_explicit_binding_and_authorizes_both_sides_before_aggregate():
    fixture = policy_fixture(bindings=(LEADS, DELIVERIES))
    repo, database = repository(fixture, bindings=(LEADS, DELIVERIES), joins=(JOIN,))
    left = request(
        binding="leads", tool="lead_list", fields=frozenset({"status", "lead_id"})
    )
    right = request(
        binding="deliveries",
        tool="delivery_list",
        fields=frozenset({"status", "delivery_id"}),
    )
    database["leads"].responses = [
        [default_document() | {"_joined": [delivery_document()]}]
    ]

    page = await repo.list_joined_records(
        fixture["principal"], left, join="lead_deliveries", right=right
    )

    pipeline = database["leads"].calls[0][0]
    lookup = pipeline[-1]["$lookup"]
    assert lookup["from"] == "deliveries"
    assert lookup["as"] == "_joined"
    assert lookup["pipeline"][-2] == {"$limit": 101}
    right_scope = lookup["pipeline"][0]["$match"]["$and"][0]
    assert {"dealership": {"$in": ["dealer-code-a"]}} in all_subdocuments(right_scope)
    assert {"$eq": ["$leadId", "$$join_value"]} in all_subdocuments(
        lookup["pipeline"][0]["$match"]
    )
    assert "privateNote" not in lookup["pipeline"][-1]["$project"]
    assert page.rows[0].left.values["status"] == "open"
    assert page.rows[0].right[0].values["status"] == "delivered"
    assert "private_note" not in page.rows[0].right[0].values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "join_name,right_request",
    [
        ("unknown", request(binding="deliveries", tool="delivery_list")),
        (
            "lead_deliveries",
            request(
                binding="deliveries",
                tool="delivery_list",
                fields=frozenset({"private_note"}),
            ),
        ),
    ],
    ids=("unknown-join", "unauthorized-right-field"),
)
async def test_unknown_or_unauthorized_join_is_denied_before_aggregate(
    join_name, right_request
):
    allowed_fields = {
        "leads": tuple(field.name for field in LEADS.fields),
        "deliveries": tuple(
            field.name for field in DELIVERIES.fields if field.name != "private_note"
        ),
    }
    fixture = policy_fixture(
        bindings=(LEADS, DELIVERIES), allowed_fields=allowed_fields
    )
    repo, database = repository(fixture, bindings=(LEADS, DELIVERIES), joins=(JOIN,))

    with pytest.raises(AccessDenied):
        await repo.list_joined_records(
            fixture["principal"],
            request(binding="leads", fields=frozenset({"status"})),
            join=join_name,
            right=right_request,
        )

    assert database.collections == {}


@pytest.mark.asyncio
async def test_joined_record_site_and_scope_are_rechecked_on_returned_rows():
    fixture = policy_fixture(bindings=(LEADS, DELIVERIES))
    repo, database = repository(fixture, bindings=(LEADS, DELIVERIES), joins=(JOIN,))
    database["leads"].responses = [
        [default_document() | {"_joined": [delivery_document(site="dealer-code-b")]}]
    ]

    with pytest.raises((AccessDenied, SourceUnavailable)):
        await repo.list_joined_records(
            fixture["principal"],
            request(binding="leads", fields=frozenset({"status"})),
            join="lead_deliveries",
            right=request(
                binding="deliveries", tool="delivery_list", fields=frozenset({"status"})
            ),
        )

    assert len(database["leads"].calls) == 1


@pytest.mark.asyncio
async def test_joined_page_combined_left_and_right_rows_cannot_exceed_100():
    fixture = policy_fixture(bindings=(LEADS, DELIVERIES))
    repo, database = repository(fixture, bindings=(LEADS, DELIVERIES), joins=(JOIN,))
    joined = [
        delivery_document() | {"deliveryId": f"delivery-{index}"}
        for index in range(100)
    ]
    database["leads"].responses = [[default_document() | {"_joined": joined}]]

    with pytest.raises(SourceUnavailable):
        await repo.list_joined_records(
            fixture["principal"],
            request(binding="leads", fields=frozenset({"status"}), page_size=1),
            join="lead_deliveries",
            right=request(
                binding="deliveries",
                tool="delivery_list",
                fields=frozenset({"status"}),
            ),
        )

    assert len(database["leads"].calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {
            "_id": MONGO_ID_1,
            "leadId": "lead-1",
            "dealership": "dealer-code-a",
            "assignedUserId": "not-an-object-id",
            "status": "open",
        },
        {
            "_id": MONGO_ID_1,
            "leadId": "lead-1",
            "dealership": "unmapped-site",
            "assignedUserId": ObjectId(STAFF_ID),
            "status": "open",
        },
        {
            "_id": "not-an-object-id",
            "leadId": "lead-1",
            "dealership": "dealer-code-a",
            "assignedUserId": ObjectId(STAFF_ID),
            "status": "open",
        },
        {
            "_id": MONGO_ID_1,
            "leadId": "lead-1",
            "dealership": "dealer-code-a",
            "assignedUserId": ObjectId(STAFF_ID),
            "status": ["open"],
        },
        {
            "_id": MONGO_ID_1,
            "leadId": "lead-1",
            "dealership": "dealer-code-a",
            "assignedUserId": ObjectId(STAFF_ID),
            "status": {"$where": "x"},
        },
    ],
    ids=(
        "bad-staff-type",
        "unmapped-site",
        "bad-mongo-id",
        "array-output",
        "object-output",
    ),
)
async def test_malformed_source_rows_fail_closed(document):
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [[document]]

    with pytest.raises((AccessDenied, SourceUnavailable)):
        await repo.list_records(fixture["principal"], request())


@pytest.mark.asyncio
async def test_non_finite_number_from_source_fails_closed():
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [[default_document() | {"score": float("nan")}]]

    with pytest.raises(SourceUnavailable):
        await repo.list_records(
            fixture["principal"], request(fields=frozenset({"amount"}))
        )


@pytest.mark.asyncio
async def test_final_authorization_change_after_recheck_prevents_page_release():
    def revoke_on_final_authorize(state, _principal):
        if state["load_count"] >= 3:
            snapshot = state["snapshot"]
            revoked_user = snapshot.user.model_copy(
                update={"approval_status": "revoked"}
            )
            state["snapshot"] = snapshot.model_copy(update={"user": revoked_user})
        return state["snapshot"]

    fixture = policy_fixture(loader=revoke_on_final_authorize)
    repo, database = repository(fixture, bindings=(LEADS,))
    database["leads"].responses = [[default_document()]]

    with pytest.raises(AccessDenied):
        await repo.list_records(fixture["principal"], request())

    assert fixture["state"]["load_count"] == 3
    assert len(database["leads"].calls) == 1


@pytest.mark.asyncio
async def test_deadline_and_cancellation_close_the_cursor(monkeypatch):
    monkeypatch.setattr(operational, "QUERY_MS", 20)
    fixture = policy_fixture()
    repo, database = repository(fixture, bindings=(LEADS,))
    timeout_cursor = FakeCursor("wait")
    database["leads"].responses = [timeout_cursor]

    with pytest.raises(SourceUnavailable):
        await repo.list_records(fixture["principal"], request())
    assert timeout_cursor.closed

    cancelled_cursor = FakeCursor("wait")
    database["leads"].responses = [cancelled_cursor]
    task = asyncio.create_task(repo.list_records(fixture["principal"], request()))
    await cancelled_cursor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_cursor.closed


@pytest.mark.asyncio
async def test_explicit_factory_uses_only_fixed_source_databases_and_closes_clients():
    clients = []

    def client_factory(uri, **options):
        assert uri == "mongodb://synthetic.invalid"
        assert options["tls"] is True
        assert options["tlsAllowInvalidCertificates"] is False
        assert options["tlsAllowInvalidHostnames"] is False
        assert options["socketTimeoutMS"] == 10_000
        client = FakeClient()
        clients.append(client)
        return client

    fixture = policy_fixture()
    repo = await connect_operational_repository(
        uris={
            "byd-leads-new": SecretStr("mongodb://synthetic.invalid"),
            "byd-panel": SecretStr("mongodb://synthetic.invalid"),
        },
        authorization=fixture["authorization"],
        bindings=(LEADS,),
        client_factory=client_factory,
    )

    assert len(clients) == 2
    assert clients[0].database_names == ["byd-leads-new"]
    assert clients[1].database_names == ["byd-panel"]
    assert all("byd_ai_layer" not in client.database_names for client in clients)
    await repo.close()
    assert [client.close_calls for client in clients] == [1, 1]
    with pytest.raises(SourceUnavailable):
        await repo.list_records(fixture["principal"], request())


@pytest.mark.asyncio
async def test_factory_rejects_non_operational_target_before_client_creation():
    fixture = policy_fixture()
    called = False

    def client_factory(*_args, **_kwargs):
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(SourceUnavailable):
        await connect_operational_repository(
            uris={"byd_ai_layer": SecretStr("mongodb://synthetic.invalid")},
            authorization=fixture["authorization"],
            client_factory=client_factory,
        )

    assert called is False
