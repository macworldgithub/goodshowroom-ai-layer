"""TICKET-002 authorization contracts with synthetic app policy only."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    CollectionPolicy,
    DataAccess,
    PolicySnapshot,
    Principal,
    ProtectedAction,
    RecordAttributes,
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

NOW = datetime(2026, 10, 23, 9, tzinfo=UTC)
CREATED = NOW - timedelta(minutes=2)
UPDATED = NOW - timedelta(minutes=1)
_DEFAULT_SITES = object()


def changed(model, **updates):
    values = {name: getattr(model, name) for name in type(model).model_fields}
    values.update(updates)
    return type(model)(**values)


def source_ref(
    collection="customers",
    *,
    database="byd-panel",
    key_field="customer_id",
    key_type="string",
    key_value="customer-42",
):
    return SourceReference(
        database=database,
        collection=collection,
        key_field=key_field,
        key_type=key_type,
        key_value=key_value,
    )


def grant(
    *,
    dataset="byd-panel",
    sites=("site_a",),
    record_mode="all_permitted",
    selected_records=(),
    fields=("site", "customer_id", "customer_name"),
    metrics=("sales_count",),
    tools=("customer_lookup",),
):
    return Grant(
        dataset=dataset,
        sites=sites,
        record_mode=record_mode,
        selected_records=selected_records,
        fields=fields,
        metrics=metrics,
        tools=tools,
    )


def user_record(user_id, role_id, scopes, **overrides):
    values = {
        "id": user_id,
        "provider_account_id": "google-subject-1",
        "email_key": "staff@example.com",
        "approval_status": "approved",
        "role_id": role_id,
        "scope_limit": scopes,
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    return UserRecord(**(values | overrides))


def role_record(role_id, grants, *, capabilities=("chat",), **overrides):
    values = {
        "id": role_id,
        "name_key": "sales_viewer",
        "active": True,
        "grants": grants,
        "capabilities": frozenset(capabilities),
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    return RoleRecord(**(values | overrides))


def login_record(user_id, login_id, *, generation=3, **overrides):
    values = {
        "id": login_id,
        "user_id": user_id,
        "generation": generation,
        "auth_started_at": CREATED,
        "created_at": CREATED,
        "updated_at": UPDATED,
        "auth_expires_at": NOW + timedelta(hours=9),
        "expires_at": NOW + timedelta(hours=7),
        "lease_valid_until": NOW + timedelta(minutes=4),
    }
    return LoginRecord(**(values | overrides))


def sites_setting(*site_ids):
    return AppSetting(
        id="sites",
        sites=tuple(SiteConfiguration(site_id=site) for site in site_ids),
        created_at=CREATED,
        updated_at=UPDATED,
    )


def mapping_for(user_id, reference=None, *, active=True, verified_at=None):
    reference = reference or source_ref(
        "users",
        key_field="_id",
        key_type="objectid",
        key_value="a" * 24,
    )
    verified_at = verified_at or UPDATED
    created_at = verified_at - timedelta(seconds=1)
    return IdentityMapping(
        id=uuid4(),
        user_id=user_id,
        source_ref=reference,
        active=active,
        sites=("site_a",),
        verified_by=uuid4(),
        verified_at=verified_at,
        created_at=created_at,
        updated_at=verified_at,
    )


def policy_fixture(
    *,
    role_grants=None,
    user_scopes=None,
    capabilities=("chat",),
    mappings=(),
    sites=_DEFAULT_SITES,
    policy_version=17,
    generation=3,
):
    allowed = role_grants or (grant(),)
    scopes = user_scopes or allowed
    user_id, role_id, login_id = uuid4(), uuid4(), uuid4()
    user = user_record(user_id, role_id, scopes)
    role = role_record(role_id, allowed, capabilities=capabilities)
    login = login_record(user_id, login_id, generation=generation)
    snapshot = PolicySnapshot(
        policy_version=policy_version,
        user=user,
        role=role,
        login=login,
        mappings=mappings,
        sites=sites_setting("site_a") if sites is _DEFAULT_SITES else sites,
    )
    return {
        "snapshot": snapshot,
        "principal": Principal(
            user_id=user_id,
            login_id=login_id,
            generation=generation,
        ),
        "identity_ok": True,
    }


def collection_policy(
    collection="customers",
    *,
    fields=("site", "customer_id", "customer_name"),
    tools=("customer_lookup",),
    metrics=("sales_count",),
    database="byd-panel",
):
    return CollectionPolicy(
        database=database,
        collection=collection,
        fields=frozenset(fields),
        tools=frozenset(tools),
        metrics=frozenset(metrics),
    )


def data_access(
    collection="customers",
    *,
    database="byd-panel",
    site="site_a",
    fields=("site", "customer_id"),
    tool="customer_lookup",
    metric=None,
    sensitive_breakdown=False,
    records=(),
):
    return DataAccess(
        database=database,
        collection=collection,
        site=site,
        fields=frozenset(fields),
        tool=tool,
        metric=metric,
        sensitive_breakdown=sensitive_breakdown,
        records=records,
    )


def action_with_data(*accesses, **overrides):
    return ProtectedAction(action="tool", data=tuple(accesses), **overrides)


def service_for(state, *, collections=None, clock=None):
    async def load_policy(_principal):
        if state.get("loader_error"):
            raise RuntimeError("private database detail")
        return state.get("snapshot")

    async def check_identity(_principal):
        return state.get("identity_ok", True)

    return AuthorizationService(
        load_policy=load_policy,
        check_identity=check_identity,
        clock=clock or (lambda: NOW),
        collections=collections if collections is not None else (collection_policy(),),
    )


async def authorize_default(state, request=None, *, collections=None, clock=None):
    return await service_for(state, collections=collections, clock=clock).authorize(
        state["principal"],
        request or action_with_data(data_access()),
    )


@pytest.mark.asyncio
async def test_approved_active_account_and_current_login_allow_a_scoped_read():
    state = policy_fixture()
    decision = await authorize_default(state)

    assert decision.policy_version == 17
    assert decision.principal == state["principal"]
    assert decision.constraints[0][0].record_mode == "all_permitted"
    assert decision.constraints[0][0].access.fields == frozenset(
        {"site", "customer_id"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change_policy",
    [
        lambda snapshot: changed(
            snapshot,
            user=changed(snapshot.user, approval_status="pending"),
        ),
        lambda snapshot: changed(
            snapshot,
            role=changed(snapshot.role, active=False),
        ),
        lambda snapshot: changed(
            snapshot,
            user=changed(snapshot.user, role_id=uuid4()),
        ),
        lambda snapshot: changed(
            snapshot,
            login=changed(snapshot.login, user_id=uuid4()),
        ),
        lambda snapshot: changed(
            snapshot,
            login=changed(snapshot.login, generation=4),
        ),
        lambda snapshot: changed(
            snapshot,
            login=changed(
                snapshot.login,
                expires_at=NOW,
                lease_valid_until=NOW,
            ),
        ),
        lambda snapshot: changed(
            snapshot,
            login=changed(
                snapshot.login,
                revoked_at=NOW,
                updated_at=NOW,
            ),
        ),
    ],
    ids=(
        "not-approved",
        "inactive-role",
        "different-account-role",
        "different-login-owner",
        "different-login-generation",
        "expired-login",
        "revoked-login",
    ),
)
async def test_unapproved_or_stale_policy_evidence_denies(change_policy):
    state = policy_fixture()
    state["snapshot"] = change_policy(state["snapshot"])

    with pytest.raises(AccessDenied, match="Access is not allowed"):
        await authorize_default(state)


@pytest.mark.asyncio
async def test_identity_registry_denial_happens_before_policy_load():
    state = policy_fixture()
    state["identity_ok"] = False
    state["loader_error"] = True

    with pytest.raises(AccessDenied):
        await authorize_default(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "unavailable"])
async def test_missing_or_unavailable_policy_denies(failure):
    state = policy_fixture()
    if failure == "missing":
        state["snapshot"] = None
    else:
        state["loader_error"] = True

    with pytest.raises(AccessDenied, match="Access is not allowed"):
        await authorize_default(state)


@pytest.mark.asyncio
async def test_user_scope_narrows_one_role_grant_and_cannot_add_fields_back():
    role_scope = grant(fields=("site", "customer_id", "customer_name"))
    user_scope = grant(fields=("site", "customer_id"))
    state = policy_fixture(role_grants=(role_scope,), user_scopes=(user_scope,))
    allowed = await authorize_default(state)
    assert allowed.constraints[0][0].access.fields == frozenset({"site", "customer_id"})

    broader = action_with_data(
        data_access(fields=("site", "customer_id", "customer_name"))
    )
    with pytest.raises(AccessDenied):
        await authorize_default(state, broader)


@pytest.mark.asyncio
async def test_user_scope_cannot_union_two_role_grants():
    role_grants = (
        grant(fields=("site", "customer_id")),
        grant(fields=("site", "customer_name")),
    )
    union_scope = grant(fields=("site", "customer_id", "customer_name"))
    state = policy_fixture(role_grants=role_grants, user_scopes=(union_scope,))

    with pytest.raises(AccessDenied):
        await authorize_default(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access,collections,sites",
    [
        (
            data_access(fields=("site", "private_field")),
            (collection_policy(),),
            sites_setting("site_a"),
        ),
        (
            data_access(tool="unregistered_tool"),
            (collection_policy(),),
            sites_setting("site_a"),
        ),
        (
            data_access(metric="private_metric"),
            (collection_policy(),),
            sites_setting("site_a"),
        ),
        (
            data_access(collection="unknown_collection"),
            (collection_policy(),),
            sites_setting("site_a"),
        ),
        (
            data_access(site="unknown_site"),
            (collection_policy(),),
            sites_setting("site_a"),
        ),
        (
            data_access(),
            (collection_policy(fields=("site",)),),
            sites_setting("site_a"),
        ),
        (
            data_access(),
            (collection_policy(tools=("other_tool",)),),
            sites_setting("site_a"),
        ),
        (
            data_access(metric="sales_count"),
            (collection_policy(metrics=()),),
            sites_setting("site_a"),
        ),
        (
            data_access(),
            (collection_policy(),),
            None,
        ),
        (
            data_access(),
            (collection_policy(),),
            sites_setting("site_b"),
        ),
    ],
    ids=(
        "field-not-in-role-grant",
        "tool-not-in-role-grant",
        "metric-not-in-role-grant",
        "collection-not-registered",
        "site-not-in-registry",
        "field-not-in-collection-registry",
        "tool-not-in-collection-registry",
        "metric-not-in-collection-registry",
        "missing-site-registry",
        "site-not-in-site-registry",
    ),
)
async def test_data_access_requires_each_explicit_grant_and_registry(
    access, collections, sites
):
    state = policy_fixture(sites=sites)

    with pytest.raises(AccessDenied):
        await authorize_default(
            state,
            action_with_data(access),
            collections=collections,
        )


@pytest.mark.asyncio
async def test_metric_is_allowed_only_when_both_role_and_collection_allow_it():
    state = policy_fixture()
    decision = await authorize_default(
        state,
        action_with_data(data_access(metric="sales_count")),
    )

    assert decision.constraints[0][0].access.metric == "sales_count"


@pytest.mark.asyncio
async def test_selected_record_grant_emits_source_qualified_selector():
    selected = source_ref()
    scoped = grant(
        record_mode="selected",
        selected_records=(selected,),
    )
    state = policy_fixture(role_grants=(scoped,), user_scopes=(scoped,))
    access = data_access(
        records=(
            RecordAttributes(
                reference=selected,
                site="site_a",
            ),
        )
    )

    decision = await authorize_default(state, action_with_data(access))
    constraint = decision.constraints[0][0]
    assert constraint.record_mode == "selected"
    assert constraint.selected_records == (selected,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        source_ref(key_value="another-customer"),
        source_ref(database="byd-leads-new"),
        source_ref(collection="appointments"),
    ],
    ids=("unknown-id", "wrong-database", "wrong-collection"),
)
async def test_selected_record_rejects_unselected_or_wrong_source(reference):
    selected = source_ref()
    scoped = grant(record_mode="selected", selected_records=(selected,))
    state = policy_fixture(role_grants=(scoped,), user_scopes=(scoped,))
    access = data_access(
        records=(RecordAttributes(reference=reference, site="site_a"),)
    )

    with pytest.raises(AccessDenied):
        await authorize_default(state, action_with_data(access))


@pytest.mark.asyncio
async def test_assigned_records_use_active_verified_source_qualified_mapping():
    staff_ref = source_ref(
        "users",
        key_field="_id",
        key_type="objectid",
        key_value="b" * 24,
    )
    assigned = grant(record_mode="assigned")
    state = policy_fixture(role_grants=(assigned,), user_scopes=(assigned,))
    mapping = mapping_for(state["principal"].user_id, staff_ref)
    state["snapshot"] = changed(state["snapshot"], mappings=(mapping,))
    access = data_access(
        records=(
            RecordAttributes(
                reference=source_ref(),
                site="site_a",
                assigned_to=(staff_ref,),
            ),
        )
    )

    decision = await authorize_default(state, action_with_data(access))
    constraint = decision.constraints[0][0]
    assert constraint.record_mode == "assigned"
    assert constraint.assigned_to == (staff_ref,)


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping_state", ["missing", "inactive", "future"])
async def test_assigned_scope_without_current_mapping_denies(mapping_state):
    staff_ref = source_ref(
        "users",
        key_field="_id",
        key_type="objectid",
        key_value="c" * 24,
    )
    assigned = grant(record_mode="assigned")
    state = policy_fixture(role_grants=(assigned,), user_scopes=(assigned,))
    mappings = ()
    if mapping_state != "missing":
        verified_at = (
            NOW + timedelta(seconds=1) if mapping_state == "future" else UPDATED
        )
        mappings = (
            mapping_for(
                state["principal"].user_id,
                staff_ref,
                active=mapping_state != "inactive",
                verified_at=verified_at,
            ),
        )
    state["snapshot"] = changed(state["snapshot"], mappings=mappings)
    access = data_access(
        records=(
            RecordAttributes(
                reference=source_ref(),
                site="site_a",
                assigned_to=(staff_ref,),
            ),
        )
    )

    with pytest.raises(AccessDenied):
        await authorize_default(state, action_with_data(access))


@pytest.mark.asyncio
async def test_unknown_assignment_cannot_match_mapping_by_key_alone():
    mapped_ref = source_ref(
        "users",
        key_field="_id",
        key_type="objectid",
        key_value="d" * 24,
    )
    lookalike_ref = source_ref(
        "users",
        database="byd-leads-new",
        key_field="_id",
        key_type="objectid",
        key_value="d" * 24,
    )
    assigned = grant(record_mode="assigned")
    state = policy_fixture(role_grants=(assigned,), user_scopes=(assigned,))
    state["snapshot"] = changed(
        state["snapshot"],
        mappings=(mapping_for(state["principal"].user_id, mapped_ref),),
    )
    access = data_access(
        records=(
            RecordAttributes(
                reference=source_ref(),
                site="site_a",
                assigned_to=(lookalike_ref,),
            ),
        )
    )

    with pytest.raises(AccessDenied):
        await authorize_default(state, action_with_data(access))


@pytest.mark.asyncio
async def test_joined_collection_has_its_own_collection_and_field_check():
    base = grant(
        fields=("site", "customer_id", "appointment_id"),
        tools=("customer_lookup", "appointment_lookup"),
    )
    state = policy_fixture(role_grants=(base,), user_scopes=(base,))
    customer_policy = collection_policy(
        "customers",
        fields=("site", "customer_id"),
        tools=("customer_lookup",),
    )
    appointment_policy = collection_policy(
        "appointments",
        fields=("site", "appointment_id"),
        tools=("appointment_lookup",),
        metrics=(),
    )
    customer = data_access(
        "customers",
        fields=("site", "customer_id"),
        records=(
            RecordAttributes(
                reference=source_ref("customers"),
                site="site_a",
            ),
        ),
    )
    appointment = data_access(
        "appointments",
        fields=("site", "appointment_id"),
        tool="appointment_lookup",
        records=(
            RecordAttributes(
                reference=source_ref("appointments", key_field="appointment_id"),
                site="site_a",
            ),
        ),
    )

    decision = await authorize_default(
        state,
        action_with_data(customer, appointment),
        collections=(customer_policy, appointment_policy),
    )
    assert len(decision.constraints) == 2
    assert decision.constraints[0][0].access.collection == "customers"
    assert decision.constraints[1][0].access.collection == "appointments"
    assert decision.constraints[1][0].access.fields == frozenset(
        {"site", "appointment_id"}
    )

    with pytest.raises(AccessDenied):
        await authorize_default(
            state,
            action_with_data(customer, appointment),
            collections=(customer_policy,),
        )


@pytest.mark.asyncio
async def test_each_joined_record_is_checked_against_its_own_source_and_site():
    base = grant(
        fields=("site", "customer_id", "appointment_id"),
        tools=("customer_lookup", "appointment_lookup"),
    )
    state = policy_fixture(role_grants=(base,), user_scopes=(base,))
    policies = (
        collection_policy("customers", fields=("site", "customer_id")),
        collection_policy(
            "appointments",
            fields=("site", "appointment_id"),
            tools=("appointment_lookup",),
            metrics=(),
        ),
    )
    customer = data_access("customers", fields=("site", "customer_id"))
    wrong_join = data_access(
        "appointments",
        fields=("site", "appointment_id"),
        tool="appointment_lookup",
        records=(
            RecordAttributes(
                reference=source_ref("customers", key_field="appointment_id"),
                site="site_a",
            ),
        ),
    )

    with pytest.raises(AccessDenied):
        await authorize_default(
            state,
            action_with_data(customer, wrong_join),
            collections=policies,
        )


@pytest.mark.asyncio
async def test_sensitive_breakdown_is_denied_without_an_approved_disclosure_rule():
    state = policy_fixture()

    with pytest.raises(AccessDenied):
        await authorize_default(
            state,
            action_with_data(
                data_access(metric="sales_count", sensitive_breakdown=True)
            ),
        )


@pytest.mark.asyncio
async def test_saved_history_is_owner_scoped_and_temporary_chat_is_login_scoped():
    state = policy_fixture()
    service = service_for(state)
    principal = state["principal"]

    own_temporary = ProtectedAction(
        action="chat",
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    assert (await service.authorize(principal, own_temporary)).request == own_temporary

    wrong_owner = changed(own_temporary, owner_id=uuid4())
    wrong_login = changed(own_temporary, chat_login_id=uuid4())
    temporary_history = changed(own_temporary, action="history")
    with pytest.raises(AccessDenied):
        await service.authorize(principal, wrong_owner)
    with pytest.raises(AccessDenied):
        await service.authorize(principal, wrong_login)
    with pytest.raises(AccessDenied):
        await service.authorize(principal, temporary_history)

    own_saved_history = ProtectedAction(
        action="history",
        owner_id=principal.user_id,
        mode="saved",
    )
    assert (await service.authorize(principal, own_saved_history)).request == (
        own_saved_history
    )


@pytest.mark.asyncio
async def test_tool_chat_context_must_match_owner_and_temporary_login_when_supplied():
    state = policy_fixture()
    service = service_for(state)
    principal = state["principal"]
    access = data_access()

    # A standalone tool can run without a chat binding.
    await service.authorize(principal, action_with_data(access))

    own_temporary_tool = action_with_data(
        access,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    await service.authorize(principal, own_temporary_tool)

    for invalid in (
        changed(own_temporary_tool, owner_id=uuid4()),
        changed(own_temporary_tool, chat_login_id=uuid4()),
    ):
        with pytest.raises(AccessDenied):
            await service.authorize(principal, invalid)


@pytest.mark.asyncio
async def test_saved_admin_review_is_separate_and_requires_its_own_capability():
    no_tools = grant(fields=("site",), metrics=(), tools=())
    state = policy_fixture(
        role_grants=(no_tools,),
        user_scopes=(no_tools,),
        capabilities=("review_saved_chats",),
    )
    principal = state["principal"]
    service = service_for(state)
    review = ProtectedAction(
        action="review_saved_chats",
        owner_id=uuid4(),
        mode="saved",
    )

    decision = await service.authorize(principal, review)
    assert decision.requires_review_audit is True

    with pytest.raises(AccessDenied):
        await service.authorize(
            principal,
            changed(review, mode="temporary"),
        )
    with pytest.raises(AccessDenied):
        await service.authorize(
            principal,
            ProtectedAction(
                action="chat",
                owner_id=principal.user_id,
                mode="saved",
            ),
        )
    with pytest.raises(AccessDenied):
        await service.authorize(
            principal,
            action_with_data(data_access()),
        )


def test_role_has_no_implicit_developer_tracing_capability():
    with pytest.raises(ValidationError):
        role_record(uuid4(), (), capabilities=("chat", "trace"))


@pytest.mark.asyncio
async def test_administrator_named_role_defaults_to_no_chat_or_review_capabilities():
    state = policy_fixture()
    snapshot = state["snapshot"]
    role = RoleRecord(
        id=snapshot.role.id,
        name_key="administrator",
        active=True,
        grants=snapshot.role.grants,
        created_at=snapshot.role.created_at,
        updated_at=snapshot.role.updated_at,
    )
    assert role.capabilities == frozenset()
    state["snapshot"] = changed(snapshot, role=role)
    service = service_for(state)
    principal = state["principal"]

    requests = (
        ProtectedAction(
            action="chat",
            owner_id=principal.user_id,
            mode="saved",
        ),
        ProtectedAction(
            action="review_saved_chats",
            owner_id=uuid4(),
            mode="saved",
        ),
    )
    for request in requests:
        with pytest.raises(AccessDenied):
            await service.authorize(principal, request)


@pytest.mark.asyncio
async def test_recheck_denies_changed_policy_account_role_and_login():
    changes = (
        lambda snapshot: changed(snapshot, policy_version=snapshot.policy_version + 1),
        lambda snapshot: changed(
            snapshot,
            user=changed(
                snapshot.user,
                approval_status="revoked",
                role_id=None,
                scope_limit=(),
            ),
        ),
        lambda snapshot: changed(
            snapshot,
            role=changed(snapshot.role, active=False),
        ),
        lambda snapshot: changed(
            snapshot,
            login=changed(
                snapshot.login,
                revoked_at=NOW,
                updated_at=NOW,
            ),
        ),
    )
    for update_policy in changes:
        state = policy_fixture()
        service = service_for(state)
        decision = await authorize_default(state)
        state["snapshot"] = update_policy(state["snapshot"])

        with pytest.raises(AccessDenied):
            await service.recheck(decision)


@pytest.mark.asyncio
async def test_recheck_detects_grant_change_without_global_version_bump():
    role_scope = grant(fields=("site", "customer_id", "customer_name"))
    state = policy_fixture(role_grants=(role_scope,), user_scopes=(role_scope,))
    service = service_for(state)
    request = action_with_data(data_access(fields=("site", "customer_id")))
    decision = await service.authorize(state["principal"], request)
    narrowed_scope = grant(fields=("site", "customer_id"))
    current_user = changed(state["snapshot"].user, scope_limit=(narrowed_scope,))
    state["snapshot"] = changed(state["snapshot"], user=current_user)

    fresh = await service.authorize(state["principal"], request)
    assert fresh.policy_version == decision.policy_version
    assert fresh.snapshot != decision.snapshot
    assert fresh.constraints == decision.constraints

    with pytest.raises(AccessDenied):
        await service.recheck(decision)


@pytest.mark.asyncio
async def test_recheck_denies_revoked_staff_mapping_before_release():
    staff_ref = source_ref(
        "users",
        key_field="_id",
        key_type="objectid",
        key_value="e" * 24,
    )
    assigned = grant(record_mode="assigned")
    state = policy_fixture(role_grants=(assigned,), user_scopes=(assigned,))
    mapping = mapping_for(state["principal"].user_id, staff_ref)
    state["snapshot"] = changed(state["snapshot"], mappings=(mapping,))
    access = data_access(
        records=(
            RecordAttributes(
                reference=source_ref(),
                site="site_a",
                assigned_to=(staff_ref,),
            ),
        )
    )
    request = action_with_data(access)
    service = service_for(state)
    decision = await service.authorize(state["principal"], request)
    state["snapshot"] = changed(
        state["snapshot"],
        mappings=(changed(mapping, active=False),),
        policy_version=18,
    )

    with pytest.raises(AccessDenied):
        await service.recheck(decision)


@pytest.mark.asyncio
async def test_recheck_denies_identity_registry_revocation():
    state = policy_fixture()
    service = service_for(state)
    decision = await authorize_default(state)
    state["identity_ok"] = False

    with pytest.raises(AccessDenied):
        await service.recheck(decision)


@pytest.mark.asyncio
async def test_policy_loader_rechecks_identity_after_its_await():
    state = policy_fixture()
    identity_checks = 0

    async def load_policy(_principal):
        state["identity_ok"] = False
        return state["snapshot"]

    async def check_identity(_principal):
        nonlocal identity_checks
        identity_checks += 1
        return state["identity_ok"]

    service = AuthorizationService(
        load_policy=load_policy,
        check_identity=check_identity,
        clock=lambda: NOW,
        collections=(collection_policy(),),
    )

    with pytest.raises(AccessDenied):
        await service.authorize(
            state["principal"],
            action_with_data(data_access()),
        )
    assert identity_checks == 2
