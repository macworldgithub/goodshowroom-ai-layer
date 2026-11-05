"""Offline TICKET-026 tests for fenced, audited access-management changes."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from goodshowroom_ai.access_management import AccessManagement
from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.authorization import Principal
from goodshowroom_ai.storage.identities import Grant
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppDatabaseUnavailable,
    AppWriteConflict,
)
from tests.test_access_request_storage import (
    AccessRequestFakeCollection,
    access_request,
    preapproval,
)
from tests.test_access_workflow import Authorization, Verifier
from tests.test_identity_storage import (
    NOW,
    login_record,
    role_record,
    selected_grant,
    user_record,
)
from tests.workflow_fake import RollbackClient


class AdminPopulationCollection(AccessRequestFakeCollection):
    """Users collection adding the bounded find() access-population read."""


def narrow_grant():
    base = selected_grant()
    return Grant(
        dataset=base.dataset,
        sites=base.sites,
        record_mode=base.record_mode,
        selected_records=base.selected_records,
        fields=("site",),
    )


def wide_grant():
    return selected_grant().model_copy(update={"sites": ("site_a", "site_b")})


def new_role(*, name=None, grants=None, capabilities=None, active=True):
    return role_record(
        name_key=name or f"role_{uuid4().hex[:10]}",
        active=active,
        capabilities=frozenset(capabilities or {"chat"}),
        grants=grants or (selected_grant(),),
    )


def store(collection, record):
    collection.documents[str(record.id)] = record.document()
    return record


def seed_user(
    client,
    *,
    role,
    scope=None,
    status="approved",
    email=None,
    provider=None,
):
    user = user_record(
        email_key=email or f"user_{uuid4().hex[:10]}@example.test",
        provider_account_id=provider or f"subject-{uuid4().hex}",
        approval_status=status,
        role_id=role.id if role else None,
        scope_limit=(scope or selected_grant(),) if status == "approved" else (),
    )
    return store(client.database["users"], user)


def policy_version(client):
    return client.database["app_settings"].documents["access_control"]["policy_version"]


def state(client):
    return {
        name: deepcopy(collection.documents)
        for name, collection in client.database.collections.items()
    }


def audit_events(client):
    return list(client.database["audit_events"].documents.values())


def refresh_decision(env):
    env.auth.decision.policy_version = policy_version(env.client)


class PerPrincipalAuthorization:
    """Snapshot per actor so concurrent changes keep their own decisions."""

    def __init__(self, decisions):
        self.decisions = decisions

    async def authorize(self, principal, request):
        assert request.action == "manage_access"
        return self.decisions[principal.user_id]

    async def recheck(self, decision):
        return decision


@pytest_asyncio.fixture
async def env():
    client = RollbackClient([])
    client.database.collections["users"] = AdminPopulationCollection("users")
    client.database.collections["roles"] = AccessRequestFakeCollection("roles")
    client.database.collections["audit_events"] = AccessRequestFakeCollection(
        "audit_events"
    )
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    await db.initialize_identity_indexes()
    await db.initialize_access_request_indexes()

    role = new_role(capabilities={"chat", "manage_access"})
    store(client.database["roles"], role)
    actor = seed_user(
        client,
        role=role,
        email="admin@example.test",
        provider="admin-subject",
    )
    login = login_record(actor.id)
    store(client.database["login_sessions"], login)
    auth = Authorization()
    auth.decision = SimpleNamespace(
        policy_version=policy_version(client),
        snapshot=SimpleNamespace(user=actor, role=role, login=login),
    )
    service = AccessManagement(db, auth, lambda: NOW)
    principal = Principal(
        user_id=actor.id, login_id=login.id, generation=login.generation
    )
    return SimpleNamespace(
        client=client,
        db=db,
        service=service,
        auth=auth,
        role=role,
        actor=actor,
        login=login,
        principal=principal,
    )


@pytest_asyncio.fixture
async def workflow_env():
    """Mirror the AccessWorkflow fixture so decide() shares its role setup."""
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    await db.initialize_identity_indexes()
    await db.initialize_access_request_indexes()
    client.database["login_sessions"]
    client.database["audit_events"]
    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    client.database["roles"].documents[str(role.id)] = role.document()
    verifier, auth = Verifier(), Authorization()
    service = AccessWorkflow(
        db,
        verifier,
        auth,
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    return SimpleNamespace(
        client=client, db=db, service=service, verifier=verifier, auth=auth, role=role
    )


@pytest.mark.asyncio
async def test_role_creation_requires_current_policy_and_audit_identity(env):
    client = env.client
    role = new_role()
    before = state(client)
    with pytest.raises(AppWriteConflict):
        await env.service.create_role(
            env.principal,
            role=role,
            expected_policy_version=policy_version(client) - 1,
            audit_request_id=uuid4(),
        )
    env.auth.decision.policy_version = policy_version(client) + 1
    with pytest.raises(AppWriteConflict):
        await env.service.create_role(
            env.principal,
            role=role,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    env.auth.decision.policy_version = policy_version(client)
    for principal, audit_request_id in (
        ("not-a-principal", uuid4()),
        (env.principal, "not-a-uuid"),
    ):
        with pytest.raises(PermissionError):
            await env.service.create_role(
                principal,
                role=role,
                expected_policy_version=policy_version(client),
                audit_request_id=audit_request_id,
            )
    assert state(client) == before

    request_id = uuid4()
    created = await env.service.create_role(
        env.principal,
        role=role,
        expected_policy_version=policy_version(client),
        audit_request_id=request_id,
    )
    assert created == role
    stored = client.database["roles"].documents[str(role.id)]
    assert stored["version"] == 0 and stored["name_key"] == role.name_key
    assert (
        policy_version(client)
        == before["app_settings"]["access_control"]["policy_version"] + 1
    )
    (event,) = audit_events(client)
    assert event["actor_id"] == str(env.actor.id)
    assert event["target_id"] == str(role.id)
    assert event["request_id"] == str(request_id)
    assert event["action"] == "access_control_change"
    assert event["outcome"] == "success"
    assert event["created_at"] == NOW


@pytest.mark.asyncio
async def test_policy_change_after_authorization_aborts_the_fence(env):
    client = env.client
    role = new_role()
    expected = policy_version(client)
    original = env.auth.authorize

    async def bump_after_authorization(principal, request):
        decision = await original(principal, request)
        client.database["app_settings"].documents["access_control"][
            "policy_version"
        ] += 1
        return decision

    env.auth.authorize = bump_after_authorization
    before = state(client)
    before["app_settings"]["access_control"]["policy_version"] = expected + 1
    with pytest.raises(AppWriteConflict):
        await env.service.create_role(
            env.principal,
            role=role,
            expected_policy_version=expected,
            audit_request_id=uuid4(),
        )
    assert state(client) == before
    assert str(role.id) not in client.database["roles"].documents
    assert not audit_events(client)


@pytest.mark.asyncio
async def test_stale_role_and_user_versions_are_rejected(env):
    client = env.client
    role = store(client.database["roles"], new_role(name="staff_chat"))
    target = seed_user(client, role=role)
    updated = role.model_copy(update={"version": 1, "updated_at": NOW})

    before = state(client)
    with pytest.raises(AppWriteConflict):
        await env.service.update_role(
            env.principal,
            role=updated,
            expected_version=1,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    with pytest.raises(AppWriteConflict):
        await env.service.change_user(
            env.principal,
            target.id,
            role_id=None,
            scope=(),
            status="revoked",
            expected_version=1,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == before
    assert client.database["users"].documents[str(target.id)]["approval_status"] == (
        "approved"
    )

    await env.service.update_role(
        env.principal,
        role=updated,
        expected_version=0,
        expected_policy_version=policy_version(client),
        audit_request_id=uuid4(),
    )
    assert client.database["roles"].documents[str(role.id)]["version"] == 1
    assert str(role.id) in client.database["roles"].documents
    assert client.database["users"].documents[str(target.id)]["role_id"] == str(role.id)
    refresh_decision(env)

    revoked = await env.service.change_user(
        env.principal,
        target.id,
        role_id=None,
        scope=(),
        status="revoked",
        expected_version=0,
        expected_policy_version=policy_version(client),
        audit_request_id=uuid4(),
    )
    assert revoked.approval_status == "revoked" and revoked.version == 1
    assert client.database["users"].documents[str(target.id)]["approval_status"] == (
        "revoked"
    )
    assert len(audit_events(client)) == 2


@pytest.mark.asyncio
async def test_concurrent_changes_with_one_version_have_one_winner(env):
    client = env.client
    role = store(client.database["roles"], new_role(name="staff_chat"))
    target = seed_user(client, role=role)
    updated = role.model_copy(update={"version": 1, "updated_at": NOW})
    expected = policy_version(client)

    async def update_role():
        return await env.service.update_role(
            env.principal,
            role=updated,
            expected_version=0,
            expected_policy_version=expected,
            audit_request_id=uuid4(),
        )

    results = await asyncio.gather(update_role(), update_role(), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert all(
        isinstance(item, AppWriteConflict)
        for item in results
        if isinstance(item, Exception)
    )
    assert client.database["roles"].documents[str(role.id)]["version"] == 1
    assert policy_version(client) == expected + 1
    assert len(audit_events(client)) == 1
    refresh_decision(env)

    expected = policy_version(client)

    async def revoke_user():
        return await env.service.change_user(
            env.principal,
            target.id,
            role_id=None,
            scope=(),
            status="revoked",
            expected_version=0,
            expected_policy_version=expected,
            audit_request_id=uuid4(),
        )

    results = await asyncio.gather(revoke_user(), revoke_user(), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert all(
        isinstance(item, AppWriteConflict)
        for item in results
        if isinstance(item, Exception)
    )
    assert client.database["users"].documents[str(target.id)]["version"] == 1
    assert client.database["users"].documents[str(target.id)]["approval_status"] == (
        "revoked"
    )
    assert policy_version(client) == expected + 1
    assert len(audit_events(client)) == 2


@pytest.mark.asyncio
async def test_scope_must_stay_within_the_role_and_the_actor(env):
    client = env.client
    target_role = store(
        client.database["roles"],
        new_role(name="target_chat", grants=(selected_grant(),)),
    )
    wide_role = store(
        client.database["roles"], new_role(name="wide_chat", grants=(wide_grant(),))
    )
    target = seed_user(client, role=target_role)
    narrow = narrow_grant()

    async def change(**overrides):
        values = dict(
            role_id=target_role.id,
            scope=(narrow,),
            status="approved",
            expected_version=0,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
        values.update(overrides)
        return await env.service.change_user(env.principal, target.id, **values)

    before = state(client)
    with pytest.raises(PermissionError):
        await change(scope=(wide_grant(),))
    with pytest.raises(PermissionError):
        await change(role_id=wide_role.id, scope=(wide_grant(),))
    with pytest.raises(PermissionError):
        await change(scope=())
    with pytest.raises(PermissionError):
        await change(scope=(narrow.model_copy(update={"sites": ("bad selector!",)}),))
    assert state(client) == before

    changed = await change(role_id=wide_role.id, scope=(narrow,))
    assert changed.role_id == wide_role.id and changed.scope_limit == (narrow,)
    stored = client.database["users"].documents[str(target.id)]
    assert stored["role_id"] == str(wide_role.id)
    assert stored["scope_limit"][0]["fields"] == ["site"]
    assert stored["scope_limit"][0]["sites"] == ["site_a"]
    assert (
        policy_version(client)
        == before["app_settings"]["access_control"]["policy_version"] + 1
    )


@pytest.mark.asyncio
async def test_roles_cannot_exceed_actor_authority_or_edit_the_actor_role(env):
    client = env.client
    wide_role = store(
        client.database["roles"], new_role(name="existing_wide", grants=(wide_grant(),))
    )
    narrow = narrow_grant()
    before = state(client)

    with pytest.raises(PermissionError):
        await env.service.create_role(
            env.principal,
            role=new_role(
                name="settings_role", capabilities={"chat", "manage_settings"}
            ),
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    with pytest.raises(PermissionError):
        await env.service.create_role(
            env.principal,
            role=new_role(name="wide_role", grants=(wide_grant(),)),
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    with pytest.raises(PermissionError):
        await env.service.update_role(
            env.principal,
            role=env.role.model_copy(update={"version": 1, "updated_at": NOW}),
            expected_version=0,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    with pytest.raises(PermissionError):
        await env.service.update_role(
            env.principal,
            role=wide_role.model_copy(
                update={"version": 1, "updated_at": NOW, "grants": (narrow,)}
            ),
            expected_version=0,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == before

    created = await env.service.create_role(
        env.principal,
        role=new_role(name="narrow_chat", grants=(narrow,)),
        expected_policy_version=policy_version(client),
        audit_request_id=uuid4(),
    )
    assert client.database["roles"].documents[str(created.id)]["grants"][0][
        "fields"
    ] == (["site"])
    assert (
        policy_version(client)
        == before["app_settings"]["access_control"]["policy_version"] + 1
    )


@pytest.mark.asyncio
async def test_self_escalation_and_self_revocation_are_denied(env):
    client = env.client
    chat_role = store(client.database["roles"], new_role(name="chat_only"))
    before = state(client)

    for role_id, scope in ((None, ()), (chat_role.id, (narrow_grant(),))):
        with pytest.raises(PermissionError):
            await env.service.change_user(
                env.principal,
                env.actor.id,
                role_id=role_id,
                scope=scope,
                status="approved" if role_id else "revoked",
                expected_version=env.actor.version,
                expected_policy_version=policy_version(client),
                audit_request_id=uuid4(),
            )
    assert state(client) == before
    actor_document = client.database["users"].documents[str(env.actor.id)]
    assert actor_document["approval_status"] == "approved"
    assert actor_document["role_id"] == str(env.role.id)
    assert (
        "manage_access"
        in client.database["roles"].documents[str(env.role.id)]["capabilities"]
    )
    assert not audit_events(client)


@pytest.mark.asyncio
async def test_concurrent_admin_revocations_keep_an_active_administrator(env):
    client = env.client
    other = seed_user(
        client,
        role=env.role,
        email="second-admin@example.test",
        provider="second-admin-subject",
    )
    other_login = store(client.database["login_sessions"], login_record(other.id))
    other_principal = Principal(
        user_id=other.id,
        login_id=other_login.id,
        generation=other_login.generation,
    )
    version = policy_version(client)
    service = AccessManagement(
        env.db,
        PerPrincipalAuthorization(
            {
                env.actor.id: SimpleNamespace(
                    policy_version=version,
                    snapshot=SimpleNamespace(
                        user=env.actor, role=env.role, login=env.login
                    ),
                ),
                other.id: SimpleNamespace(
                    policy_version=version,
                    snapshot=SimpleNamespace(
                        user=other, role=env.role, login=other_login
                    ),
                ),
            }
        ),
        lambda: NOW,
    )

    async def revoke(principal, user):
        return await service.change_user(
            principal,
            user.id,
            role_id=None,
            scope=(),
            status="revoked",
            expected_version=user.version,
            expected_policy_version=version,
            audit_request_id=uuid4(),
        )

    results = await asyncio.gather(
        revoke(env.principal, other),
        revoke(other_principal, env.actor),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert all(
        isinstance(item, (PermissionError, AppWriteConflict))
        for item in results
        if isinstance(item, Exception)
    )
    administrators = [
        row
        for row in client.database["users"].documents.values()
        if row["approval_status"] == "approved"
        and row["role_id"] == str(env.role.id)
        and row["scope_limit"]
    ]
    assert len(administrators) == 1
    assert str(env.role.id) in client.database["roles"].documents
    assert len(audit_events(client)) == 1
    assert policy_version(client) == version + 1


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_role_and_user_changes(env):
    client = env.client
    target_role = store(client.database["roles"], new_role(name="staff_chat"))
    target = seed_user(client, role=target_role)
    audits = client.database["audit_events"]

    audits.fail_insert = True
    before = state(client)
    with pytest.raises(AppDatabaseUnavailable):
        await env.service.create_role(
            env.principal,
            role=new_role(),
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == before
    with pytest.raises(AppDatabaseUnavailable):
        await env.service.change_user(
            env.principal,
            target.id,
            role_id=None,
            scope=(),
            status="revoked",
            expected_version=0,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == before

    audits.fail_insert = False
    role = await env.service.create_role(
        env.principal,
        role=new_role(name="recovered"),
        expected_policy_version=policy_version(client),
        audit_request_id=uuid4(),
    )
    assert str(role.id) in client.database["roles"].documents
    assert len(audit_events(client)) == 1


@pytest.mark.asyncio
async def test_preapproval_create_and_revoke_bump_policy_and_audit(env):
    client = env.client
    role = store(
        client.database["roles"],
        new_role(name="preapproved_chat", grants=(selected_grant(),)),
    )
    narrow = narrow_grant()
    record = preapproval("staff@example.test", role, scope=narrow)
    request_id = uuid4()
    before = policy_version(client)

    created = await env.service.create_preapproval(
        env.principal,
        record=record,
        expected_policy_version=before,
        audit_request_id=request_id,
    )
    assert created.id == record.id
    assert created.kind == record.kind == "preapproval"
    assert created.email_key == record.email_key
    assert created.user_id is record.user_id is None
    assert created.status == record.status == "pending"
    assert created.active is record.active is True
    assert created.role_id == record.role_id == role.id
    assert created.scope_limit == record.scope_limit == (narrow,)
    assert created.expires_at == record.expires_at
    assert created.created_at == NOW and created.updated_at == NOW
    assert created.reviewed_by == env.actor.id
    assert created.reviewed_at == NOW
    stored = client.database["access_requests"].documents[str(record.id)]
    assert stored["status"] == "pending" and stored["active"] is True
    assert stored["user_id"] is None
    assert stored["reviewed_by"] == str(env.actor.id)
    assert stored["reviewed_at"] == NOW
    assert policy_version(client) == before + 1
    (event,) = audit_events(client)
    assert event["actor_id"] == str(env.actor.id)
    assert event["target_id"] == str(record.id)
    assert event["request_id"] == str(request_id)
    refresh_decision(env)

    revoke_request_id = uuid4()
    before_revoke = policy_version(client)
    await env.service.revoke_preapproval(
        env.principal,
        record.id,
        expected_policy_version=before_revoke,
        audit_request_id=revoke_request_id,
    )
    revoked = client.database["access_requests"].documents[str(record.id)]
    assert revoked["status"] == "revoked" and revoked["active"] is False
    assert revoked["reviewed_by"] == str(env.actor.id)
    assert revoked["reviewed_at"] == NOW
    assert policy_version(client) == before_revoke + 1
    assert len(audit_events(client)) == 2
    assert audit_events(client)[-1]["request_id"] == str(revoke_request_id)
    refresh_decision(env)

    before_repeat = state(client)
    with pytest.raises(AppWriteConflict):
        await env.service.revoke_preapproval(
            env.principal,
            record.id,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == before_repeat


@pytest.mark.asyncio
async def test_preapproval_timestamps_come_from_the_trusted_clock(env):
    client = env.client
    role = store(
        client.database["roles"],
        new_role(name="preapproved_chat", grants=(selected_grant(),)),
    )
    earlier = NOW - timedelta(minutes=1)
    record = preapproval("staff@example.test", role, scope=narrow_grant()).model_copy(
        update={"created_at": earlier, "updated_at": earlier}
    )
    before = policy_version(client)

    created = await env.service.create_preapproval(
        env.principal,
        record=record,
        expected_policy_version=before,
        audit_request_id=uuid4(),
    )

    assert created.created_at == NOW and created.updated_at == NOW
    stored = client.database["access_requests"].documents[str(record.id)]
    assert stored["created_at"] == NOW and stored["updated_at"] == NOW
    assert stored["status"] == "pending" and stored["active"] is True
    assert policy_version(client) == before + 1
    assert len(audit_events(client)) == 1


@pytest.mark.asyncio
async def test_preapproval_requires_current_policy_and_bounded_scope(env):
    client = env.client
    role = store(
        client.database["roles"],
        new_role(name="preapproved_chat", grants=(selected_grant(),)),
    )
    inactive_role = store(
        client.database["roles"],
        new_role(name="inactive_chat", grants=(selected_grant(),), active=False),
    )
    privileged_role = store(
        client.database["roles"],
        new_role(
            name="privileged_role",
            grants=(selected_grant(),),
            capabilities={"chat", "manage_settings"},
        ),
    )
    target = seed_user(client, role=None, status="pending")
    narrow = narrow_grant()
    before = state(client)

    async def create(record, *, expected_policy_version, error):
        with pytest.raises(error):
            await env.service.create_preapproval(
                env.principal,
                record=record,
                expected_policy_version=expected_policy_version,
                audit_request_id=uuid4(),
            )

    stale = preapproval("staff@example.test", role, scope=narrow)
    await create(
        stale,
        expected_policy_version=policy_version(client) - 1,
        error=AppWriteConflict,
    )
    await create(
        preapproval("staff@example.test", role, scope=wide_grant()),
        expected_policy_version=policy_version(client),
        error=PermissionError,
    )
    await create(
        preapproval("staff@example.test", inactive_role, scope=narrow),
        expected_policy_version=policy_version(client),
        error=PermissionError,
    )
    await create(
        preapproval("staff@example.test", privileged_role, scope=narrow),
        expected_policy_version=policy_version(client),
        error=PermissionError,
    )
    await create(
        access_request(target),
        expected_policy_version=policy_version(client),
        error=PermissionError,
    )
    assert state(client) == before

    request = store(client.database["access_requests"], access_request(target))
    request_state = state(client)
    with pytest.raises(AppWriteConflict):
        await env.service.revoke_preapproval(
            env.principal,
            request.id,
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    with pytest.raises(PermissionError):
        await env.service.revoke_preapproval(
            env.principal,
            "not-a-uuid",
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == request_state


@pytest.mark.asyncio
async def test_decide_cannot_exceed_administrator_scope_or_capabilities(workflow_env):
    client = workflow_env.client
    actor = user_record(
        approval_status="approved",
        role_id=workflow_env.role.id,
        scope_limit=(selected_grant(),),
    )
    target = user_record(
        provider_account_id="target-subject", email_key="target@example.test"
    )
    login = login_record(actor.id)
    request = access_request(target)
    for name, record in [
        ("users", actor),
        ("users", target),
        ("login_sessions", login),
        ("access_requests", request),
    ]:
        client.database[name].documents[str(record.id)] = record.document()
    principal = Principal(
        user_id=actor.id, login_id=login.id, generation=login.generation
    )
    workflow_env.auth.decision = SimpleNamespace(
        policy_version=policy_version(client),
        snapshot=SimpleNamespace(user=actor, role=workflow_env.role, login=login),
    )
    privileged_role = store(
        client.database["roles"],
        new_role(
            name="privileged_target",
            grants=(selected_grant(),),
            capabilities={"chat", "manage_settings"},
        ),
    )
    wider = selected_grant().model_copy(update={"sites": ("site_a", "site_b")})
    before = state(client)

    async def decide(**overrides):
        values = dict(
            approve=True,
            role_id=workflow_env.role.id,
            scope=(narrow_grant(),),
            expected_policy_version=policy_version(client),
            audit_request_id=uuid4(),
        )
        values.update(overrides)
        return await workflow_env.service.decide(principal, request.id, **values)

    with pytest.raises(PermissionError):
        await decide(role_id=privileged_role.id)
    with pytest.raises(PermissionError):
        await decide(scope=(wider,))
    assert state(client) == before
    assert (
        client.database["users"].documents[str(target.id)]["approval_status"]
        == "pending"
    )
    assert (
        client.database["access_requests"].documents[str(request.id)]["status"]
        == "pending"
    )
