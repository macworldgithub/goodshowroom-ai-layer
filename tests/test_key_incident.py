"""Synthetic compromise tests through real storage and assertion boundaries."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.agent_service import QuestionPlan
from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    PolicySnapshot,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.identity import (
    AssertionVerifier,
    IdentityDenied,
    VerificationKey,
    operation_digest,
)
from goodshowroom_ai.identity_security import (
    IdentitySecurityState,
    public_key_fingerprint,
)
from goodshowroom_ai.key_incident import contain_signing_key_compromise
from goodshowroom_ai.storage.models import (
    InternalAuthorizationContext,
    StorageCapability,
)
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
)
from goodshowroom_ai.temporary import TemporaryUnavailable
from tests.test_access_workflow import Authorization, version
from tests.test_identity_bridge import (
    AUDIENCE,
    ISSUER,
    KID,
    _bootstrap_claims,
    _mint,
    _public_pem,
    _verify,
    _no_login,
)
from tests.test_identity_storage import NOW, role_record, selected_grant
from tests.test_temporary import create_context, setup_store
from tests.test_agent_service import _setup, ScriptedRunner
from tests.workflow_fake import RollbackClient


def verifier_for(db, key, *, kid=KID):
    return AssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys={kid: VerificationKey(_public_pem(key), NOW - timedelta(minutes=1))},
        load_security=db.load_identity_security,
    )


async def setup_incident(key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    await db.initialize_identity_indexes()
    await db.initialize_access_request_indexes()
    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    client.database["roles"].documents[str(role.id)] = role.document()
    verifier = verifier_for(db, key)
    now = [NOW]
    workflow = AccessWorkflow(
        db,
        verifier,
        Authorization(),
        lambda: now[0],
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    path = "/v1/internal/login/exchange"
    claims = _bootstrap_claims(
        path=path, digest=operation_digest("POST", path), email="admin@example.test"
    )
    user, login = await workflow.exchange(
        _mint(key, claims), expected_policy_version=version(client), request_id=uuid4()
    )
    return SimpleNamespace(
        client=client,
        db=db,
        verifier=verifier,
        workflow=workflow,
        user=user,
        login=login,
        claims=claims,
        now=now,
    )


async def contain(env, *, memory=None, kid=KID, expected=None, now=NOW):
    return await contain_signing_key_compromise(
        env.db,
        env.verifier,
        memory or SimpleNamespace(close=AsyncMock()),
        kid=kid,
        actor_id=env.user.id,
        request_id=uuid4(),
        expected_policy_version=version(env.client) if expected is None else expected,
        now=now,
    )


@pytest.mark.asyncio
async def test_durable_rejection_survives_restart_and_key_rename(signing_key):
    env = await setup_incident(signing_key)
    other_process = verifier_for(env.db, signing_key)
    before = deepcopy(env.client.database["login_sessions"].documents)
    state = await contain(env, now=NOW + timedelta(microseconds=500))
    assert state.auth_cutoff == int(NOW.timestamp())
    assert state.revoked_key_ids == (KID,)
    assert env.verifier.locked
    assert (
        env.client.database["login_sessions"].documents == before
    )  # Replay bindings remain.
    restarted_db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: env.client
    )
    for verifier, kid in (
        (other_process, KID),
        (verifier_for(restarted_db, signing_key), KID),
        (verifier_for(restarted_db, signing_key, kid="renamed"), "renamed"),
    ):
        with pytest.raises(IdentityDenied):
            await _verify(verifier, _mint(signing_key, kid=kid), load_login=_no_login)
    assert await restarted_db.load_identity_security() == state
    context = InternalAuthorizationContext(
        actor_id=env.user.id, capabilities=frozenset({StorageCapability.READ_LOGINS})
    )
    async with restarted_db.snapshot_transaction(context) as tx:
        with pytest.raises(AuthorizationDenied):
            await tx.get_login(env.login.id)
    write_context = InternalAuthorizationContext(
        actor_id=env.user.id,
        capabilities=frozenset(
            {
                StorageCapability.WRITE_LOGINS,
                StorageCapability.ADVANCE_ACCESS_CONTROL,
                StorageCapability.WRITE_REQUIRED_AUDIT,
            }
        ),
    )
    with pytest.raises(AuthorizationDenied):
        async with restarted_db.transaction(write_context) as tx:
            await tx.advance_access_control_fence(version(env.client))
            await tx.renew_login(
                env.login.id,
                generation=env.login.generation,
                expected_version=env.login.version,
                now=NOW,
                verified_auth_expiry=env.login.auth_expires_at,
            )
    audit = list(env.client.database["audit_events"].documents.values())[-1]
    assert audit["action"] == "security_outcome" and audit["target_id"] is None
    assert audit["actor_id"] == str(env.user.id)
    assert "key" not in str(audit) and "token" not in str(audit)


@pytest.mark.asyncio
async def test_healthy_key_cannot_rebind_old_login_but_new_signin_works(signing_key):
    env = await setup_incident(signing_key)
    await contain(env)
    healthy = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    verifier = verifier_for(env.db, healthy, kid="healthy")
    env.now[0] = NOW + timedelta(seconds=2)
    workflow = AccessWorkflow(
        env.db,
        verifier,
        Authorization(),
        lambda: env.now[0],
        "admin@example.test",
        env.user.role_id,
        (selected_grant(),),
    )
    for session_id in (str(env.login.id), str(uuid4())):
        with pytest.raises((IdentityDenied, AuthorizationDenied)):
            await workflow.exchange(
                _mint(
                    healthy,
                    env.claims | {"login_session_id": session_id},
                    kid="healthy",
                ),
                expected_policy_version=version(env.client),
                request_id=uuid4(),
            )
    stamp = int(env.now[0].timestamp())
    fresh = env.claims | {
        "login_session_id": str(uuid4()),
        "iat": stamp,
        "nbf": stamp,
        "exp": stamp + 120,
        "auth_started_at": stamp,
    }
    user, login = await workflow.exchange(
        _mint(healthy, fresh, kid="healthy"),
        expected_policy_version=version(env.client),
        request_id=uuid4(),
    )
    assert user.id == env.user.id and login.id != env.login.id
    assert login.valid(env.now[0])


@pytest.mark.asyncio
async def test_revocation_while_verifier_waits_cannot_release_claims(signing_key):
    env = await setup_incident(signing_key)
    verifier = verifier_for(env.db, signing_key)

    async def during_lookup(_identifier):
        await contain(env)
        return None

    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key, _bootstrap_claims()),
            purpose="bootstrap",
            load_login=during_lookup,
        )


@pytest.mark.asyncio
async def test_policy_recheck_and_saved_commit_login_gate_deny_old_generation(
    signing_key,
):
    env = await setup_incident(signing_key)
    principal = Principal(
        user_id=env.user.id, login_id=env.login.id, generation=env.login.generation
    )

    async def load_policy(_principal):
        context = InternalAuthorizationContext(
            actor_id=env.user.id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_LOGINS,
                    StorageCapability.READ_POLICY,
                    StorageCapability.READ_IDENTITIES,
                }
            ),
        )
        async with env.db.snapshot_transaction(context) as tx:
            return PolicySnapshot(
                policy_version=await tx.get_policy_version(),
                user=env.user,
                role=await tx.get_role(env.user.role_id),
                login=await tx.get_login(env.login.id),
            )

    service = AuthorizationService(
        load_policy=load_policy,
        check_identity=AsyncMock(return_value=True),
        clock=lambda: NOW,
    )
    decision = await service.authorize(
        principal, ProtectedAction(action="manage_access")
    )
    await contain(env)
    with pytest.raises(AccessDenied):
        await service.recheck(decision)
    context = InternalAuthorizationContext(
        actor_id=env.user.id,
        capabilities=frozenset(
            {
                StorageCapability.READ_LOGINS,
                StorageCapability.READ_POLICY,
                StorageCapability.ADVANCE_ACCESS_CONTROL,
                StorageCapability.WRITE_REQUIRED_AUDIT,
            }
        ),
    )
    with pytest.raises(AppWriteConflict):
        async with env.db.transaction(context) as tx:
            await tx.advance_access_control_fence(decision.policy_version)


@pytest.mark.asyncio
async def test_incident_cancels_temporary_work_and_denies_late_memory_write(
    signing_key,
):
    env = await setup_incident(signing_key)
    principal, store, _clock, _auth, _fixture = setup_store()
    _chat, context, command = await create_context(store, principal)
    started = asyncio.Event()

    async def running():
        await store.begin_turn(principal, context, command)
        started.set()
        await asyncio.Event().wait()

    work = asyncio.create_task(running())
    await asyncio.wait_for(started.wait(), 2)
    await contain(env, memory=store)
    with pytest.raises(asyncio.CancelledError):
        await work
    with pytest.raises(TemporaryUnavailable):
        await store.load(principal, context, command)
    assert not store._entries


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_incident_and_keeps_process_locked(signing_key):
    env = await setup_incident(signing_key)
    before = deepcopy(env.client.database["app_settings"].documents)
    env.client.database["audit_events"].fail_insert = True
    memory = SimpleNamespace(close=AsyncMock())
    with pytest.raises(AppDatabaseUnavailable):
        await contain(env, memory=memory)
    assert env.client.database["app_settings"].documents == before
    assert env.verifier.locked
    memory.close.assert_awaited_once()
    with pytest.raises(IdentityDenied):
        await _verify(env.verifier, _mint(signing_key))
    env.client.database["audit_events"].fail_insert = False
    await contain(env, memory=memory)
    assert (await env.db.load_identity_security()).auth_cutoff is not None


@pytest.mark.asyncio
async def test_stale_incident_and_cleanup_failure_never_report_success(signing_key):
    env = await setup_incident(signing_key)
    with pytest.raises(AppWriteConflict):
        await contain(env, expected=99999)
    assert env.verifier.locked
    assert (await env.db.load_identity_security()).auth_cutoff is None
    with pytest.raises(RuntimeError, match="synthetic"):
        await contain(
            env,
            memory=SimpleNamespace(
                close=AsyncMock(side_effect=RuntimeError("synthetic"))
            ),
        )
    assert (await env.db.load_identity_security()).auth_cutoff is None


@pytest.mark.asyncio
async def test_incident_is_not_available_to_signed_login_storage_context(signing_key):
    env = await setup_incident(signing_key)
    context = InternalAuthorizationContext(
        actor_id=env.user.id,
        capabilities=frozenset(
            {
                StorageCapability.READ_POLICY,
                StorageCapability.ADVANCE_ACCESS_CONTROL,
                StorageCapability.WRITE_REQUIRED_AUDIT,
                StorageCapability.WRITE_LOGINS,
            }
        ),
    )
    with pytest.raises(AuthorizationDenied):
        async with env.db.transaction(context) as tx:
            await tx.advance_access_control_fence(version(env.client))
            await tx.revoke_identity_key(
                kid=KID,
                fingerprint=public_key_fingerprint(signing_key.public_key()),
                now=NOW,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"auth_cutoff": True},
        {"revoked_key_ids": ["bad key"]},
        {"auth_cutoff": 1},
        {"unexpected": True},
    ],
)
async def test_malformed_durable_state_denies_assertions_and_logins(signing_key, raw):
    env = await setup_incident(signing_key)
    env.client.database["app_settings"].documents["access_control"][
        "identity_security"
    ] = raw
    with pytest.raises(AppDatabaseUnavailable):
        await env.db.load_identity_security()
    with pytest.raises(IdentityDenied):
        await _verify(env.verifier, _mint(signing_key))


@pytest.mark.asyncio
async def test_security_read_and_confirmation_failure_deny_success(
    signing_key, monkeypatch
):
    env = await setup_incident(signing_key)

    async def unavailable(_self):
        raise AppDatabaseUnavailable("synthetic")

    monkeypatch.setattr(AppDatabase, "load_identity_security", unavailable)
    with pytest.raises(IdentityDenied):
        await _verify(verifier_for(env.db, signing_key), _mint(signing_key))
    with pytest.raises(AppDatabaseUnavailable):
        await contain(env)
    assert env.verifier.locked
    # A readback failure does not roll back the already committed response.
    assert (
        env.client.database["app_settings"].documents["access_control"][
            "identity_security"
        ]["auth_cutoff"]
        is not None
    )


def test_state_rejects_duplicate_ids_and_subsecond_logins():
    digest = "a" * 64
    with pytest.raises(ValidationError):
        IdentitySecurityState(
            revoked_key_ids=("one", "one"), revoked_public_keys=(digest,), auth_cutoff=1
        )
    state = IdentitySecurityState(
        revoked_key_ids=("one",),
        revoked_public_keys=(digest,),
        auth_cutoff=int(NOW.timestamp()),
    )
    assert not state.allows_login(NOW + timedelta(microseconds=999999))
    assert state.allows_login(NOW + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_concurrent_incidents_conflict_then_retry_preserves_both_keys(
    signing_key,
):
    env = await setup_incident(signing_key)
    healthy = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    env.verifier.keys["second"] = VerificationKey(
        _public_pem(healthy), NOW - timedelta(minutes=1)
    )
    expected = version(env.client)
    results = await asyncio.gather(
        contain(env, expected=expected),
        contain(env, kid="second", expected=expected),
        return_exceptions=True,
    )
    assert sum(isinstance(item, IdentitySecurityState) for item in results) == 1
    assert sum(isinstance(item, AppWriteConflict) for item in results) == 1
    await contain(env, kid="second", now=NOW + timedelta(seconds=3))
    await contain(env, now=NOW)
    state = await env.db.load_identity_security()
    assert set(state.revoked_key_ids) == {KID, "second"}
    assert state.auth_cutoff == int((NOW + timedelta(seconds=3)).timestamp())


@pytest.mark.asyncio
async def test_running_saved_agent_cannot_commit_or_release_after_incident(signing_key):
    env = await setup_incident(signing_key)
    fixture, service, repository, responses, _tracing, principal, context, command = (
        _setup()
    )
    snapshot = fixture["state"]["snapshot"]
    authorization = fixture["authorization"]

    async def identity_current(_principal):
        return (await env.db.load_identity_security()).allows_login(
            snapshot.login.auth_started_at
        )

    authorization._check_identity = identity_current
    # Use the agent fixture's clock; all existing app logins predate this incident.
    incident_time = snapshot.login.created_at + timedelta(minutes=3)
    original = responses.build_metric

    async def complete_after_incident(*args):
        built = await original(*args)
        await contain(env, now=incident_time)
        return built

    responses.build_metric = complete_after_incident
    service._runner = ScriptedRunner(
        QuestionPlan(intent="count_leads", site="site_a"),
        tool_arguments={"site": "site_a"},
    )
    with pytest.raises(AccessDenied):
        await service.run_turn(principal, context, command)
    assert len(responses.metric_calls) == 1
    assert repository.commit_calls == []
    assert repository.fail_calls[0][4] == "authorization_changed"


@pytest.mark.asyncio
async def test_incident_write_requires_majority_snapshot_and_bounded_commit(
    signing_key, monkeypatch
):
    env = await setup_incident(signing_key)
    session = env.client.session
    original = type(session).start_transaction
    options = []

    async def capture(self, **kwargs):
        options.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(type(session), "start_transaction", capture)
    await contain(env)
    assert options[0]["read_concern"].document == {"level": "snapshot"}
    assert options[0]["write_concern"].document == {"w": "majority"}
    assert options[0]["max_commit_time_ms"] == 5000


@pytest.mark.asyncio
async def test_key_removal_during_final_registry_read_denies_claim_release(signing_key):
    env = await setup_incident(signing_key)
    calls = 0

    async def read_and_remove():
        nonlocal calls
        state = await env.db.load_identity_security()
        calls += 1
        if calls == 2:
            env.verifier.keys.pop(KID)
        return state

    env.verifier._load_security = read_and_remove
    with pytest.raises(IdentityDenied):
        await _verify(
            env.verifier,
            _mint(signing_key, _bootstrap_claims()),
            purpose="bootstrap",
            load_login=_no_login,
        )
    assert calls == 2
