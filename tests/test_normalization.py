"""Synthetic source normalization and authorization checks for TICKET-004."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from bson import ObjectId

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    CollectionPolicy,
    PolicySnapshot,
    Principal,
)
from goodshowroom_ai.normalization import (
    IdentityColumn,
    LeadAdapterConfig,
    LeadNormalizationService,
    PlatformDateRule,
    StaffColumn,
    build_lead_adapter,
    resolve_relationship,
    resolve_site,
    source_reference,
)
from goodshowroom_ai.operational import OperationalReadRepository, SourceUnavailable
from goodshowroom_ai.operational_models import (
    DateFilter,
    PagePosition,
    ReadPage,
    ReadRequest,
    ReadRow,
)
from goodshowroom_ai.source_dates import DateRule
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
from tests.test_operational_reads import FakeCollection, FakeDatabase


NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
CREATED = NOW - timedelta(minutes=10)
UPDATED = NOW - timedelta(minutes=1)
USER_ID = UUID("00000000-0000-0000-0000-000000000101")
ROLE_ID = UUID("00000000-0000-0000-0000-000000000102")
LOGIN_ID = UUID("00000000-0000-0000-0000-000000000103")
ADMIN_ID = UUID("00000000-0000-0000-0000-000000000104")
STAFF_ID = "b" * 24


def date_rule(provenance="original"):
    return DateRule(
        provenance=provenance,
        review_reference="scope-date-review-2026-10-22",
        local_timezone="Australia/Melbourne",
    )


def lead_config(*, date_rules=None, staff=True, identities=True):
    return LeadAdapterConfig(
        binding="leads_v1",
        site_columns=("dealer", "dealerName"),
        identities=(
            (
                IdentityColumn(
                    column="leadId",
                    key_field="leadId",
                    key_type="string",
                ),
                IdentityColumn(
                    column="_id",
                    key_field="_id",
                    key_type="objectid",
                ),
            )
            if identities
            else ()
        ),
        staff=(
            (
                StaffColumn(
                    column="assignedUserId",
                    collection="users",
                    key_field="_id",
                    key_type="objectid",
                    review_reference="scope-staff-review-2026-10-22",
                ),
            )
            if staff
            else ()
        ),
        date_rules=(
            (PlatformDateRule(platform="virtualyard", rule=date_rule()),)
            if date_rules is None
            else date_rules
        ),
    )


def site_settings():
    return AppSetting(
        id="sites",
        sites=(
            SiteConfiguration(
                site_id="site_a",
                aliases=("North showroom",),
                timezone="Australia/Melbourne",
            ),
            SiteConfiguration(
                site_id="site_b",
                aliases=("South showroom",),
                timezone="Australia/Sydney",
            ),
        ),
        created_at=CREATED,
        updated_at=NOW,
    )


def policy_snapshot(
    config,
    *,
    additional_fields=(),
    policy_version=17,
    grant_sites=("site_a",),
    mappings=(),
):
    grant = Grant(
        dataset="byd-leads-new",
        sites=grant_sites,
        record_mode="all_permitted",
        fields=tuple(sorted(config.required_fields | frozenset(additional_fields))),
        tools=("lead_table",),
    )
    user = UserRecord(
        id=USER_ID,
        provider_account_id="synthetic-account-001",
        email_key="synthetic.user@example.test",
        approval_status="approved",
        role_id=ROLE_ID,
        scope_limit=(grant,),
        created_at=CREATED,
        updated_at=UPDATED,
    )
    role = RoleRecord(
        id=ROLE_ID,
        name_key="sales_viewer",
        active=True,
        grants=(grant,),
        capabilities=frozenset({"chat"}),
        created_at=CREATED,
        updated_at=UPDATED,
    )
    login = LoginRecord(
        id=LOGIN_ID,
        user_id=USER_ID,
        generation=3,
        auth_started_at=NOW - timedelta(minutes=20),
        created_at=CREATED,
        updated_at=UPDATED,
        auth_expires_at=NOW + timedelta(hours=2),
        expires_at=NOW + timedelta(hours=1),
        lease_valid_until=NOW + timedelta(minutes=3),
    )
    return PolicySnapshot(
        policy_version=policy_version,
        user=user,
        role=role,
        login=login,
        mappings=tuple(mappings),
        sites=site_settings(),
    )


def lead_reference(value="a" * 24):
    return SourceReference(
        database="byd-leads-new",
        collection="leads",
        key_field="_id",
        key_type="objectid",
        key_value=value,
    )


def lead_row(*, reference=None, **overrides):
    values = {
        "platform": "virtualyard",
        "leadCreatedDate": datetime(2026, 10, 25, 1, tzinfo=UTC),
        "status": "new",
        "stage": "NEW ENQUIRIES",
        "dealer": "north showroom",
        "dealerName": None,
        "leadId": "lead-synthetic-001",
        "_id": "a" * 24,
        "assignedUserId": STAFF_ID,
    }
    values.update(overrides)
    return ReadRow(reference=reference or lead_reference(), values=values)


def read_request(config, *, fields=None, site="site_a", page_size=25, dates=None):
    return ReadRequest(
        binding=config.binding,
        site=site,
        tool="lead_table",
        fields=config.required_fields if fields is None else fields,
        dates=dates,
        page_size=page_size,
    )


def next_position():
    return PagePosition(
        user_id=USER_ID,
        policy_version=17,
        request_digest="0" * 64,
        after=lead_reference("c" * 24),
    )


def read_page(*rows, policy_version=17, continuation=None):
    return ReadPage(
        rows=tuple(rows),
        next_position=continuation,
        policy_version=policy_version,
    )


class AuthorizationHarness:
    """A real AuthorizationService with only synthetic policy and identity."""

    def __init__(
        self,
        config,
        *,
        snapshot=None,
        policy_schedule=None,
        additional_fields=(),
    ):
        extra_fields = frozenset(additional_fields)
        self.snapshot = snapshot or policy_snapshot(
            config, additional_fields=extra_fields
        )
        self.policy_schedule = policy_schedule
        self.load_calls = 0

        async def load_policy(_principal):
            self.load_calls += 1
            if self.policy_schedule is not None:
                index = min(self.load_calls - 1, len(self.policy_schedule) - 1)
                return self.policy_schedule[index]
            return self.snapshot

        async def check_identity(_principal):
            return True

        self.service = AuthorizationService(
            load_policy=load_policy,
            check_identity=check_identity,
            clock=lambda: NOW,
            collections=(
                CollectionPolicy(
                    database="byd-leads-new",
                    collection="leads",
                    fields=config.required_fields | extra_fields,
                    tools=frozenset({"lead_table"}),
                ),
            ),
        )
        self.principal = Principal(
            user_id=USER_ID,
            login_id=LOGIN_ID,
            generation=3,
        )


class RepositoryStub:
    def __init__(self, page, *, on_read=None):
        self.page = page
        self.on_read = on_read
        self.calls = []

    async def list_records(self, principal, request, *, position=None):
        self.calls.append((principal, request, position))
        if self.on_read is not None:
            self.on_read()
        return self.page


def normalization_service(config, repository, authorization):
    return LeadNormalizationService(
        repository=repository,
        authorization=authorization.service,
        config=config,
        clock=lambda: NOW,
    )


def operational_lead_document(source_date_value):
    return {
        "_id": ObjectId("000000000000000000000201"),
        "platform": "virtualyard",
        "leadCreatedDate": source_date_value,
        "status": "new",
        "stage": "NEW ENQUIRIES",
        "dealer": "North showroom",
        "dealerName": None,
        "leadId": "lead-e2e-synthetic-001",
        "virtualyardId": "virtualyard-e2e-synthetic-001",
        "autogateLeadId": None,
        "customerId": None,
    }


def operational_repository(binding, authorization, source_date_value):
    database = FakeDatabase("byd-leads-new")
    collection = FakeCollection([[operational_lead_document(source_date_value)]])
    database.collections["leads"] = collection
    repository = OperationalReadRepository(
        databases={"byd-leads-new": database},
        authorization=authorization.service,
        bindings=(binding,),
    )
    return repository, collection


def staff_mapping(
    *,
    user_id=USER_ID,
    active=True,
    sites=("site_a",),
    verified_at=None,
    value=STAFF_ID,
):
    verified_at = verified_at or NOW - timedelta(minutes=2)
    return IdentityMapping(
        id=uuid4(),
        user_id=user_id,
        source_ref=SourceReference(
            database="byd-leads-new",
            collection="users",
            key_field="_id",
            key_type="objectid",
            key_value=value,
        ),
        active=active,
        sites=sites,
        verified_by=ADMIN_ID,
        verified_at=verified_at,
        created_at=verified_at - timedelta(minutes=1),
        updated_at=verified_at + timedelta(minutes=1),
    )


def test_source_reference_preserves_configured_string_and_objectid_types():
    hex_value = "0123456789abcdef01234567"

    string_reference = source_reference(
        database="byd-leads-new",
        collection="leads",
        key_field="leadId",
        key_type="string",
        value=hex_value,
    )
    objectid_reference = source_reference(
        database="byd-leads-new",
        collection="leads",
        key_field="_id",
        key_type="objectid",
        value=ObjectId(hex_value),
    )

    assert string_reference.key_type == "string"
    assert string_reference.key_value == hex_value
    assert objectid_reference.key_type == "objectid"
    assert objectid_reference.key_value == hex_value
    assert string_reference != objectid_reference


def test_compiled_lead_adapter_uses_only_configured_safe_lead_fields():
    binding, config = build_lead_adapter(site_settings())
    fields_by_path = {field.path: field for field in binding.fields}

    assert binding.database == "byd-leads-new"
    assert binding.collection == "leads"
    assert binding.identity == "record_id"
    assert fields_by_path["_id"].kind == "objectid"
    assert fields_by_path["leadCreatedDate"].kind == "source_date"
    assert all(
        fields_by_path[field].kind == "string"
        for field in (
            "platform",
            "status",
            "stage",
            "dealer",
            "dealerName",
            "leadId",
            "virtualyardId",
            "autogateLeadId",
            "customerId",
        )
    )
    assert set(fields_by_path) == set(config.required_fields) | {"_id"}
    assert config.staff == ()
    assert config.date_rules == ()
    assert all(identity.key_type == "string" for identity in config.identities)
    assert binding.sites[0].values == ("site_a", "North showroom")
    assert binding.sites[1].values == ("site_b", "South showroom")
    assert not {
        "name",
        "phone",
        "homePhone",
        "email",
        "allocatedPersonFullName",
        "createdAt",
        "updatedAt",
        "leadDate",
        "testDrive",
        "testDriveDate",
        "scrapedAt",
    } & set(fields_by_path)


@pytest.mark.parametrize("settings_kind", ["missing", "empty-sites", "wrong-setting"])
def test_compiled_lead_adapter_rejects_missing_or_unusable_settings(settings_kind):
    if settings_kind == "missing":
        settings = None
    elif settings_kind == "empty-sites":
        settings = AppSetting(
            id="sites",
            sites=(),
            created_at=CREATED,
            updated_at=NOW,
        )
    else:
        settings = AppSetting(
            id="retention",
            saved_retention_days=30,
            created_at=CREATED,
            updated_at=NOW,
        )

    with pytest.raises(SourceUnavailable, match="^Source is unavailable\\.$"):
        build_lead_adapter(settings)


@pytest.mark.parametrize(
    "alias",
    ["all", "All Locations", "  all locations  "],
    ids=("all", "all-locations", "trimmed-all-locations"),
)
def test_compiled_lead_adapter_rejects_wide_site_aliases(alias):
    settings = AppSetting(
        id="sites",
        sites=(
            SiteConfiguration(
                site_id="site_a",
                aliases=(alias,),
                timezone="Australia/Melbourne",
            ),
        ),
        created_at=CREATED,
        updated_at=NOW,
    )

    with pytest.raises(SourceUnavailable, match="^Source is unavailable\\.$"):
        build_lead_adapter(settings)


@pytest.mark.parametrize(
    ("source_value", "expected_state", "expected_instant", "expected_calendar"),
    [
        (
            datetime(2026, 1, 15, 2, 30, tzinfo=UTC),
            "valid",
            datetime(2026, 1, 15, 2, 30, tzinfo=UTC),
            None,
        ),
        (
            "2026-01-15T12:30:00+10:00",
            "valid",
            datetime(2026, 1, 15, 2, 30, tzinfo=UTC),
            None,
        ),
        (datetime(2026, 1, 15, 2, 30), "timezone_missing", None, None),
        ("2026-01-15T12:30:00", "timezone_missing", None, None),
        ("2024-02-29", "date_only", None, date(2024, 2, 29)),
        (None, "missing", None, None),
        ("not-a-date", "invalid", None, None),
    ],
    ids=(
        "aware-bson-date",
        "offset-iso",
        "naive-date",
        "naive-iso",
        "date-only",
        "null",
        "invalid-text",
    ),
)
def test_compiled_adapter_and_real_repository_preserve_source_date_shape(
    source_value, expected_state, expected_instant, expected_calendar
):
    binding, config = build_lead_adapter(site_settings())
    harness = AuthorizationHarness(
        config,
        additional_fields=(binding.identity,),
    )
    repository, collection = operational_repository(binding, harness, source_value)

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    evidence = page.rows[0].date
    assert collection.calls
    assert collection.write_calls == []
    assert evidence.state == expected_state
    assert evidence.candidate_at == expected_instant
    assert evidence.calendar_date == expected_calendar
    assert evidence.provenance == "unverified"
    assert evidence.received_at is None
    assert page.coverage.received_verified == 0
    assert page.coverage.received_unavailable == 1


def test_real_repository_rejects_non_date_source_value_without_raw_error_detail():
    binding, config = build_lead_adapter(site_settings())
    harness = AuthorizationHarness(
        config,
        additional_fields=(binding.identity,),
    )
    invalid_source_value = {"untrusted": "synthetic-invalid-date"}
    repository, collection = operational_repository(
        binding, harness, invalid_source_value
    )

    with pytest.raises(SourceUnavailable) as error:
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, read_request(config)
            )
        )

    assert str(error.value) == "Source is unavailable."
    assert "synthetic-invalid-date" not in str(error.value)
    assert collection.calls
    assert collection.write_calls == []


def test_source_date_field_cannot_be_used_as_a_raw_date_filter():
    binding, config = build_lead_adapter(site_settings())
    harness = AuthorizationHarness(
        config,
        additional_fields=(binding.identity,),
    )
    repository, collection = operational_repository(
        binding, harness, datetime(2026, 10, 25, 1, tzinfo=UTC)
    )
    request = read_request(
        config,
        dates=DateFilter(
            field="leadCreatedDate",
            start=datetime(2026, 10, 1, tzinfo=UTC),
            end=datetime(2026, 10, 26, tzinfo=UTC),
        ),
    )

    with pytest.raises(SourceUnavailable):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, request
            )
        )

    assert collection.calls == []
    assert collection.write_calls == []


@pytest.mark.parametrize(
    ("key_field", "key_type", "value"),
    [
        ("leadId", "string", ObjectId("0123456789abcdef01234567")),
        ("phone", "string", "555-0101"),
        ("displayName", "string", "Synthetic Staff"),
        ("email", "string", "synthetic.user@example.test"),
        ("leadId", "string", 123),
    ],
    ids=(
        "objectid-is-not-string",
        "phone-is-not-key",
        "name-is-not-key",
        "email-is-not-key",
        "number-is-not-string",
    ),
)
def test_source_reference_rejects_wrong_type_and_nonstable_identity_fields(
    key_field, key_type, value
):
    with pytest.raises(ValueError):
        source_reference(
            database="byd-leads-new",
            collection="leads",
            key_field=key_field,
            key_type=key_type,
            value=value,
        )


def test_source_references_with_equal_values_stay_source_and_field_qualified():
    string_ref = source_reference(
        database="byd-panel",
        collection="customers",
        key_field="customer_id",
        key_type="string",
        value="same-synthetic-id",
    )
    wrong_database = string_ref.model_copy(update={"database": "byd-leads-new"})
    wrong_collection = string_ref.model_copy(update={"collection": "clients"})
    wrong_field = string_ref.model_copy(update={"key_field": "id"})

    assert len({string_ref, wrong_database, wrong_collection, wrong_field}) == 4


def test_dealer_studio_is_a_known_platform_and_keeps_independent_status_and_stage():
    config = lead_config(date_rules=())
    harness = AuthorizationHarness(config)
    repository = RepositoryStub(read_page(lead_row(platform="dealer_studio")))

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    assert page.rows[0].platform == "dealer_studio"
    assert page.rows[0].status == "new"
    assert page.rows[0].stage == "new_enquiries"
    assert page.rows[0].date.provenance == "unverified"
    assert page.rows[0].date.received_at is None


@pytest.mark.parametrize(
    ("status", "stage", "expected_status", "expected_stage"),
    [
        ("sold", "LOST", "sold", "lost"),
        ("pending", "DELIVERED", None, "delivered"),
        ("DELIVERED", "NEW ENQUIRIES", None, "new_enquiries"),
    ],
    ids=(
        "independent-values",
        "pending-is-not-inferred",
        "stage-does-not-become-status",
    ),
)
def test_status_and_stage_are_normalized_independently_without_pending_inference(
    status, stage, expected_status, expected_stage
):
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    repository = RepositoryStub(read_page(lead_row(status=status, stage=stage)))

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    assert page.rows[0].status == expected_status
    assert page.rows[0].stage == expected_stage
    assert ("unknown_status" in page.rows[0].warnings) is (expected_status is None)


def test_site_resolves_only_configured_aliases_and_canonical_values():
    result = resolve_site((" North Showroom ",), site_settings())

    assert result.state == "verified"
    assert result.site == "site_a"


@pytest.mark.parametrize(
    "values",
    [
        ("north showroom", "site_b"),
        ("unmapped showroom",),
        ("All Sites",),
        ("all",),
        ("all locations",),
        ("*",),
        (123,),
    ],
    ids=(
        "conflicting-sites",
        "unmapped",
        "all-sites",
        "all",
        "all-locations",
        "wildcard",
        "wrong-type",
    ),
)
def test_conflicting_unmapped_or_wide_site_values_do_not_resolve(values):
    result = resolve_site(values, site_settings())

    assert result.state in {"ambiguous", "unknown"}
    assert result.site is None


def test_missing_site_value_stays_missing():
    assert resolve_site((None, ""), site_settings()).state == "missing"


@pytest.mark.parametrize(
    "qualifier",
    ["inactive", "wrong-user", "wrong-site", "future"],
)
def test_staff_assignment_requires_active_current_user_mapped_site_and_current_time(
    qualifier,
):
    import asyncio

    mapping = staff_mapping(
        active=qualifier != "inactive",
        user_id=uuid4() if qualifier == "wrong-user" else USER_ID,
        sites=("site_b",) if qualifier == "wrong-site" else ("site_a",),
        verified_at=(NOW + timedelta(minutes=1))
        if qualifier == "future"
        else NOW - timedelta(minutes=2),
    )
    config = lead_config()
    harness = AuthorizationHarness(
        config,
        snapshot=policy_snapshot(config, mappings=(mapping,)),
    )
    repository = RepositoryStub(read_page(lead_row()))

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    assert page.rows[0].assigned_to == ()
    assert "ownership_unverified" in page.rows[0].warnings


def test_active_staff_mapping_for_current_user_and_site_can_resolve_assignment():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(
        config,
        snapshot=policy_snapshot(config, mappings=(staff_mapping(),)),
    )
    repository = RepositoryStub(read_page(lead_row()))

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    assert page.rows[0].assigned_to == (staff_mapping().source_ref,)
    assert "ownership_unverified" not in page.rows[0].warnings


def test_staff_names_and_phone_values_do_not_create_identity():
    import asyncio

    config = lead_config()
    mapping = staff_mapping(value="c" * 24)
    harness = AuthorizationHarness(
        config,
        snapshot=policy_snapshot(config, mappings=(mapping,)),
    )
    repository = RepositoryStub(
        read_page(
            lead_row(
                assignedUserId=None,
                allocatedPersonFullName="Synthetic Staff Member",
                assignedPhone="555-0102",
            )
        )
    )

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    assert page.rows[0].assigned_to == ()
    assert "ownership_unverified" in page.rows[0].warnings


def test_normalization_uses_only_lead_created_date_and_default_provenance_is_unknown():
    import asyncio

    config = lead_config(date_rules=())
    harness = AuthorizationHarness(config)
    fallback_date = datetime(2026, 10, 20, 8, tzinfo=UTC)
    repository = RepositoryStub(
        read_page(
            lead_row(
                platform="dealer_studio",
                leadCreatedDate=None,
                leadDate=fallback_date,
                createdAt=fallback_date,
                scrapeDate=fallback_date,
                testDriveDate=fallback_date,
            )
        )
    )

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    evidence = page.rows[0].date
    assert evidence.field == "leadCreatedDate"
    assert evidence.state == "missing"
    assert evidence.provenance == "unverified"
    assert evidence.candidate_at is None
    assert evidence.received_at is None
    assert page.coverage.received_verified == 0
    assert page.coverage.received_unavailable == 1


def test_request_must_use_every_exact_configured_normalization_field():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    repository = RepositoryStub(read_page(lead_row()))
    wrong_fields = config.required_fields - {"stage"}

    with pytest.raises(SourceUnavailable):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal,
                read_request(config, fields=wrong_fields),
            )
        )

    assert repository.calls == []


def test_repository_receives_the_exact_configured_field_set():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    repository = RepositoryStub(read_page(lead_row()))

    asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal, read_request(config)
        )
    )

    assert len(repository.calls) == 1
    assert repository.calls[0][1].fields == config.required_fields


def test_authorization_denies_before_repository_read_when_site_is_out_of_scope():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(
        config,
        snapshot=policy_snapshot(config, grant_sites=("site_b",)),
    )
    repository = RepositoryStub(read_page(lead_row()))

    with pytest.raises(AccessDenied):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, read_request(config)
            )
        )

    assert repository.calls == []


def test_stale_repository_policy_version_is_denied_before_normalization():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    repository = RepositoryStub(read_page(lead_row(), policy_version=18))

    with pytest.raises(AccessDenied):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, read_request(config)
            )
        )


def test_policy_change_during_repository_read_is_denied_before_release():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    changed_snapshot = harness.snapshot.model_copy(update={"policy_version": 18})
    repository = RepositoryStub(
        read_page(lead_row()),
        on_read=lambda: setattr(harness, "snapshot", changed_snapshot),
    )

    with pytest.raises(AccessDenied):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, read_request(config)
            )
        )


def test_policy_change_after_recheck_is_denied_before_normalized_release():
    import asyncio

    config = lead_config()
    initial = policy_snapshot(config)
    changed_snapshot = initial.model_copy(update={"policy_version": 18})
    harness = AuthorizationHarness(
        config,
        snapshot=initial,
        policy_schedule=(initial, initial, changed_snapshot),
    )
    repository = RepositoryStub(read_page(lead_row()))

    with pytest.raises(AccessDenied):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, read_request(config)
            )
        )

    assert harness.load_calls == 3


def test_duplicate_source_references_in_one_page_are_denied():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    duplicate = lead_row()
    repository = RepositoryStub(read_page(duplicate, duplicate))

    with pytest.raises(SourceUnavailable):
        asyncio.run(
            normalization_service(config, repository, harness).read_page(
                harness.principal, read_request(config)
            )
        )


def test_coverage_describes_only_this_authorized_page_and_makes_no_total_claim():
    import asyncio

    config = lead_config()
    harness = AuthorizationHarness(config)
    continuation = next_position()
    repository = RepositoryStub(read_page(lead_row(), continuation=continuation))

    page = asyncio.run(
        normalization_service(config, repository, harness).read_page(
            harness.principal,
            read_request(config, page_size=1),
            position=continuation,
        )
    )

    assert page.coverage.scope == "returned_page"
    assert page.coverage.inspected == len(page.rows) == 1
    assert page.coverage.received_verified == 1
    assert page.next_position == continuation
    assert not hasattr(page.coverage, "total")
    assert not hasattr(page, "total")


def panel_reference(
    collection, key_field, value, *, key_type="string", database="byd-panel"
):
    return SourceReference(
        database=database,
        collection=collection,
        key_field=key_field,
        key_type=key_type,
        key_value=value,
    )


RELATIONS = (
    (
        "opportunity_customer",
        "opportunities",
        "customer_id",
        "customers",
        "customer_id",
    ),
    ("opportunity_delivery", "opportunities", "delivery_client_id", "clients", "id"),
    (
        "opportunity_sales_log",
        "opportunities",
        "sales_log_id",
        "saleslogentries",
        "sales_log_id",
    ),
    ("customer_delivery", "customers", "delivery_client_id", "clients", "id"),
)


@pytest.mark.parametrize(
    ("relation", "source_collection", "column", "target_collection", "target_field"),
    RELATIONS,
    ids=(
        "opportunity-customer",
        "opportunity-delivery",
        "opportunity-sales-log",
        "customer-delivery",
    ),
)
def test_only_four_known_panel_relations_match_authorized_candidates(
    relation, source_collection, column, target_collection, target_field
):
    value = "target-synthetic-001"
    source = ReadRow(
        reference=panel_reference(source_collection, "id", "source-synthetic-001"),
        values={column: value},
    )
    target = ReadRow(
        reference=panel_reference(target_collection, target_field, value),
        values={},
    )

    evidence = resolve_relationship(
        source,
        relation=relation,
        column=column,
        authorized_targets=(target,),
    )

    assert evidence.state == "matched"
    assert evidence.target == target.reference


def test_relationship_with_no_source_value_is_missing():
    source = ReadRow(
        reference=panel_reference("opportunities", "id", "source-synthetic-002"),
        values={"customer_id": None},
    )

    evidence = resolve_relationship(
        source,
        relation="opportunity_customer",
        column="customer_id",
        authorized_targets=(),
    )

    assert evidence.state == "missing"
    assert evidence.target is None


@pytest.mark.parametrize("value", [123, True, ObjectId("0123456789abcdef01234567")])
def test_relationship_identifier_of_wrong_type_is_invalid(value):
    source = ReadRow.model_construct(
        reference=panel_reference("opportunities", "id", "source-synthetic-003"),
        values={"customer_id": value},
    )

    evidence = resolve_relationship(
        source,
        relation="opportunity_customer",
        column="customer_id",
        authorized_targets=(),
    )

    assert evidence.state == "invalid"
    assert evidence.target is None


def test_unmatched_relationship_is_unresolved_not_reported_as_nonexistent():
    source = ReadRow(
        reference=panel_reference("opportunities", "id", "source-synthetic-004"),
        values={"customer_id": "customer-not-in-authorized-page"},
    )

    evidence = resolve_relationship(
        source,
        relation="opportunity_customer",
        column="customer_id",
        authorized_targets=(),
    )

    assert evidence.state == "unresolved"
    assert evidence.target is None


@pytest.mark.parametrize(
    "target_reference",
    [
        panel_reference(
            "customers",
            "customer_id",
            "0123456789abcdef01234567",
            database="byd-leads-new",
        ),
        panel_reference("clients", "customer_id", "0123456789abcdef01234567"),
        panel_reference("customers", "id", "0123456789abcdef01234567"),
        panel_reference(
            "customers",
            "customer_id",
            "0123456789abcdef01234567",
            key_type="objectid",
        ),
    ],
    ids=("wrong-database", "wrong-collection", "wrong-key-field", "wrong-key-type"),
)
def test_same_value_with_wrong_source_qualification_does_not_match(target_reference):
    source = ReadRow(
        reference=panel_reference("opportunities", "id", "source-synthetic-005"),
        values={"customer_id": "0123456789abcdef01234567"},
    )
    target = ReadRow(reference=target_reference, values={})

    evidence = resolve_relationship(
        source,
        relation="opportunity_customer",
        column="customer_id",
        authorized_targets=(target,),
    )

    assert evidence.state == "unresolved"
    assert evidence.target is None


def test_duplicate_authorized_candidates_are_ambiguous():
    source = ReadRow(
        reference=panel_reference("opportunities", "id", "source-synthetic-006"),
        values={"customer_id": "customer-synthetic-006"},
    )
    target = ReadRow(
        reference=panel_reference("customers", "customer_id", "customer-synthetic-006"),
        values={},
    )

    evidence = resolve_relationship(
        source,
        relation="opportunity_customer",
        column="customer_id",
        authorized_targets=(target, target),
    )

    assert evidence.state == "ambiguous"
    assert evidence.target is None


@pytest.mark.parametrize(
    ("relation", "source_reference"),
    [
        (
            "unknown_relation",
            panel_reference("opportunities", "id", "source-synthetic-007"),
        ),
        (
            "opportunity_customer",
            panel_reference(
                "opportunities", "id", "source-synthetic-008", database="byd-leads-new"
            ),
        ),
        (
            "opportunity_customer",
            panel_reference("customers", "id", "source-synthetic-009"),
        ),
    ],
    ids=("unknown-relation", "wrong-database", "wrong-collection"),
)
def test_unknown_or_wrong_source_relationship_is_denied(relation, source_reference):
    source = ReadRow(reference=source_reference, values={"customer_id": "x"})

    with pytest.raises(SourceUnavailable):
        resolve_relationship(
            source,
            relation=relation,
            column="customer_id",
            authorized_targets=(),
        )


def test_relationship_column_must_match_the_reviewed_relation_field():
    source = ReadRow(
        reference=panel_reference("opportunities", "id", "source-synthetic-010"),
        values={"customer_id": "customer-synthetic-010"},
    )

    with pytest.raises(SourceUnavailable):
        resolve_relationship(
            source,
            relation="opportunity_customer",
            column="displayName",
            authorized_targets=(),
        )
