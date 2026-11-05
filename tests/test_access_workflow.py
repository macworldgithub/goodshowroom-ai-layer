"""Offline access workflow tests using synthetic identities and isolated state."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.app import create_app
from goodshowroom_ai.authorization import AccessDenied, Principal
from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppDatabaseUnavailable,
    AppWriteConflict,
)
from tests.test_access_request_storage import access_request, preapproval
from tests.test_identity_storage import (
    NOW,
    login_record,
    role_record,
    selected_grant,
    user_record,
)
from tests.workflow_fake import RollbackClient
from tests.test_identity_bridge import _bootstrap_claims, _mint, _verifier, KID
from goodshowroom_ai.identity import operation_digest
from dataclasses import replace


class Verifier:
    def __init__(self, email="admin@example.test", subject="subject-1"):
        self.claims = {
            "email": email,
            "provider_account_id": subject,
            "login_session_id": str(uuid4()),
        }
        self.valid = True

    async def verify(self, token, **kwargs):
        assert kwargs["purpose"] == "bootstrap"
        if not self.valid or token != "verified":
            raise AccessDenied("Access is not allowed.")
        return dict(self.claims)


class Authorization:
    def __init__(self):
        self.decision = None
        self.allowed = True

    async def authorize(self, principal, request):
        assert request.action == "manage_access"
        if not self.allowed:
            raise AccessDenied("Access is not allowed.")
        return self.decision

    async def recheck(self, decision):
        if not self.allowed:
            raise AccessDenied("Access is not allowed.")
        return decision


@pytest_asyncio.fixture
async def env():
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
    return client, service, verifier, auth, role


def version(client):
    return client.database["app_settings"].documents["access_control"]["policy_version"]


def state(client):
    return {
        name: deepcopy(col.documents)
        for name, col in client.database.collections.items()
    }


async def resolve(service, client, token="verified"):
    return await service.resolve(
        token, expected_policy_version=version(client), request_id=uuid4()
    )


@pytest.mark.asyncio
async def test_bootstrap_verified_once_and_collision_denied(env):
    client, service, verifier, _, _ = env
    with pytest.raises(PermissionError):
        await resolve(service, client, "unverified")
    user = await resolve(service, client)
    assert user.approval_status == "approved"
    assert (
        client.database["app_settings"].documents["access_control"][
            "bootstrap_complete"
        ]
        is True
    )
    assert (await resolve(service, client)).id == user.id
    before = state(client)
    verifier.claims["provider_account_id"] = "other-subject"
    with pytest.raises(PermissionError):
        await resolve(service, client)
    assert state(client) == before


@pytest.mark.asyncio
async def test_bootstrap_competing_attempts_have_one_winner(env):
    client, service, _, _, _ = env
    expected = version(client)

    async def attempt():
        return await service.resolve(
            "verified", expected_policy_version=expected, request_id=uuid4()
        )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert len(client.database["users"].documents) == 1
    assert (
        client.database["app_settings"].documents["access_control"][
            "bootstrap_complete"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_missing_bootstrap_state_denies_and_rolls_back(env):
    client, service, _, _, _ = env
    del client.database["app_settings"].documents["access_control"][
        "bootstrap_complete"
    ]
    before = state(client)
    with pytest.raises(Exception):
        await resolve(service, client)
    assert state(client) == before


@pytest.mark.asyncio
async def test_expired_preapproval_does_not_grant_access(env):
    from datetime import timedelta

    client, service, verifier, _, role = env
    verifier.claims.update(
        email="staff@example.test", provider_account_id="staff-subject"
    )
    pre = preapproval("staff@example.test", role, expires_at=NOW - timedelta(seconds=1))
    client.database["access_requests"].documents[str(pre.id)] = pre.document()
    user = await resolve(service, client)
    assert user.approval_status == "pending"
    assert (
        client.database["access_requests"].documents[str(pre.id)]["status"] == "pending"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [True, False])
async def test_bootstrap_preapproval_must_match_configured_grant(env, matching):
    client, service, _, _, role = env
    other_role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    client.database["roles"].documents[str(other_role.id)] = other_role.document()
    pre = preapproval("admin@example.test", role if matching else other_role)
    client.database["access_requests"].documents[str(pre.id)] = pre.document()
    before = state(client)
    if matching:
        result = await resolve(service, client)
        assert result.approval_status == "approved"
        assert (
            client.database["access_requests"].documents[str(pre.id)]["status"]
            == "consumed"
        )
    else:
        with pytest.raises(PermissionError):
            await resolve(service, client)
        assert state(client) == before


@pytest.mark.asyncio
async def test_real_signed_assertion_and_revoked_key_rolls_back(env, signing_key):
    client, _, _, auth, role = env
    verifier = _verifier(signing_key)
    path = "/v1/internal/login/exchange"
    claims = _bootstrap_claims(
        path=path, digest=operation_digest("POST", path), email="admin@example.test"
    )
    token = _mint(signing_key, claims)

    class RevokingVerifier:
        def __init__(self):
            self.calls = 0

        async def verify(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 3:
                verifier.keys[KID] = replace(verifier.keys[KID], revoked=True)
            return await verifier.verify(*args, **kwargs)

    service = AccessWorkflow(
        client_db := await AppDatabase.connect(
            "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
        ),
        RevokingVerifier(),
        auth,
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    before = state(client)
    with pytest.raises(PermissionError):
        await resolve(service, client, token)
    assert state(client) == before
    verifier.keys[KID] = replace(verifier.keys[KID], revoked=False)
    service = AccessWorkflow(
        client_db,
        verifier,
        auth,
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    assert (await resolve(service, client, token)).approval_status == "approved"


@pytest.mark.asyncio
async def test_signed_exchange_creates_one_login_and_replay_keeps_lease(
    env, signing_key
):
    client, _, _, auth, role = env
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    service = AccessWorkflow(
        db,
        _verifier(signing_key),
        auth,
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    path = "/v1/internal/login/exchange"
    claims = _bootstrap_claims(
        path=path, digest=operation_digest("POST", path), email="admin@example.test"
    )
    token = _mint(signing_key, claims)
    user, login = await service.exchange(
        token, expected_policy_version=version(client), request_id=uuid4()
    )
    assert user.approval_status == "approved"
    assert login.user_id == user.id
    assert str(login.id) == claims["login_session_id"]
    assert len(client.database["login_sessions"].documents) == 1
    again, replay = await service.exchange(
        token, expected_policy_version=version(client), request_id=uuid4()
    )
    assert again.id == user.id
    assert replay == login
    assert len(client.database["login_sessions"].documents) == 1
    before = state(client)
    collision = claims | {"provider_account_id": "other-google-subject"}
    with pytest.raises(PermissionError):
        await service.exchange(
            _mint(signing_key, collision),
            expected_policy_version=version(client),
            request_id=uuid4(),
        )
    assert state(client) == before
    stored = client.database["login_sessions"].documents[str(login.id)]
    stored["revoked_at"] = NOW
    before = state(client)
    with pytest.raises(PermissionError):
        await service.exchange(
            token, expected_policy_version=version(client), request_id=uuid4()
        )
    assert state(client) == before


@pytest.mark.asyncio
async def test_signed_exchange_rolls_back_identity_and_login_on_audit_failure(
    env, signing_key
):
    client, _, _, auth, role = env
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    service = AccessWorkflow(
        db,
        _verifier(signing_key),
        auth,
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    path = "/v1/internal/login/exchange"
    claims = _bootstrap_claims(
        path=path, digest=operation_digest("POST", path), email="admin@example.test"
    )
    before = state(client)
    client.database["audit_events"].fail_insert = True
    with pytest.raises(AppDatabaseUnavailable, match="Required audit write failed"):
        await service.exchange(
            _mint(signing_key, claims),
            expected_policy_version=version(client),
            request_id=uuid4(),
        )
    assert state(client) == before


@pytest.mark.asyncio
async def test_exchange_http_requires_valid_operation_bound_signature(env, signing_key):
    client, _, _, auth, role = env
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    service = AccessWorkflow(
        db,
        _verifier(signing_key),
        auth,
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    app = create_app(Settings(), login_workflow=service)
    path = "/v1/internal/login/exchange"
    claims = _bootstrap_claims(
        path=path, digest=operation_digest("POST", path), email="admin@example.test"
    )
    token = _mint(signing_key, claims)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        for case, kwargs in enumerate(
            (
                {},
                {"headers": {"authorization": "Bearer invalid"}},
                {"headers": {"authorization": f"Bearer {token}"}, "content": b"{}"},
            )
        ):
            response = await http.post(path, **kwargs)
            assert response.status_code == (403 if case == 1 else 401)
            assert token not in response.text
        response = await http.post(path, headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()["data"]
        assert data["login_session_id"] == claims["login_session_id"]
        assert data["access_status"] == "approved"
        assert token not in response.text
        replay = await http.post(path, headers={"authorization": f"Bearer {token}"})
        assert replay.status_code == 200
        assert replay.json()["data"] == data
        assert len(client.database["login_sessions"].documents) == 1


@pytest.mark.asyncio
async def test_preapproval_closes_request_and_rolls_back_on_audit_failure(env):
    client, service, verifier, _, role = env
    verifier.claims.update(
        email="staff@example.test", provider_account_id="staff-subject"
    )
    user = user_record(
        email_key="staff@example.test", provider_account_id="staff-subject"
    )
    request, pre = access_request(user), preapproval(user.email_key, role)
    client.database["users"].documents[str(user.id)] = user.document()
    for item in (request, pre):
        client.database["access_requests"].documents[str(item.id)] = item.document()
    before = state(client)
    client.database["audit_events"].fail_insert = True
    with pytest.raises(Exception):
        await resolve(service, client)
    assert state(client) == before
    client.database["audit_events"].fail_insert = False
    result = await resolve(service, client)
    assert result.approval_status == "approved" and result.role_id == role.id
    assert (
        client.database["access_requests"].documents[str(pre.id)]["status"]
        == "consumed"
    )
    assert (
        client.database["access_requests"].documents[str(request.id)]["status"]
        == "approved"
    )


@pytest.mark.asyncio
async def test_request_expiry_after_audit_rolls_back(env):
    from datetime import timedelta

    client, service, _, auth, role = env
    actor = user_record(
        approval_status="approved", role_id=role.id, scope_limit=(selected_grant(),)
    )
    target = user_record(provider_account_id="target", email_key="target@example.test")
    login = login_record(actor.id)
    request = access_request(target, expires_at=NOW + timedelta(seconds=1))
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
    auth.decision = SimpleNamespace(
        policy_version=version(client),
        snapshot=SimpleNamespace(user=actor, role=role, login=login),
    )
    clock = [NOW]
    service._clock = lambda: clock[0]

    async def advance_after_change(decision):
        clock[0] = NOW + timedelta(seconds=1)
        return decision

    auth.recheck = advance_after_change
    before = state(client)
    with pytest.raises(AppWriteConflict):
        await service.decide(
            principal,
            request.id,
            approve=True,
            role_id=role.id,
            scope=(selected_grant(),),
            expected_policy_version=version(client),
            audit_request_id=uuid4(),
        )
    assert state(client) == before


@pytest.mark.asyncio
async def test_decisions_require_current_authority_and_valid_scope(env):
    client, service, _, auth, role = env
    actor = user_record(
        approval_status="approved", role_id=role.id, scope_limit=(selected_grant(),)
    )
    target = user_record(provider_account_id="target", email_key="target@example.test")
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
    auth.decision = SimpleNamespace(
        policy_version=version(client),
        snapshot=SimpleNamespace(user=actor, role=role, login=login),
    )

    async def decide(**overrides):
        values = dict(
            approve=True,
            role_id=role.id,
            scope=(selected_grant(),),
            expected_policy_version=version(client),
            audit_request_id=uuid4(),
        )
        values.update(overrides)
        return await service.decide(principal, request.id, **values)

    before = state(client)
    auth.allowed = False
    with pytest.raises(AccessDenied):
        await decide()
    auth.allowed = True
    with pytest.raises(AppWriteConflict):
        await decide(expected_policy_version=version(client) - 1)
    with pytest.raises(PermissionError):
        await decide(scope=())
    wider = selected_grant().model_copy(update={"sites": ("site_a", "site_b")})
    with pytest.raises(PermissionError):
        await decide(scope=(wider,))
    forged = selected_grant().model_copy(update={"sites": ("bad selector!",)})
    with pytest.raises((PermissionError, ValueError)):
        await decide(scope=(forged,))
    assert state(client) == before
    self_request = access_request(actor)
    client.database["access_requests"].documents[str(self_request.id)] = (
        self_request.document()
    )
    with pytest.raises(PermissionError):
        await service.decide(
            principal,
            self_request.id,
            approve=True,
            role_id=role.id,
            scope=(selected_grant(),),
            expected_policy_version=version(client),
            audit_request_id=uuid4(),
        )
    del client.database["access_requests"].documents[str(self_request.id)]
    assert state(client) == before
    original_authorize = auth.authorize

    async def revoke_after_authorization(principal, request):
        decision = await original_authorize(principal, request)
        client.database["login_sessions"].documents[str(login.id)]["revoked_at"] = NOW
        return decision

    auth.authorize = revoke_after_authorization
    with pytest.raises(PermissionError):
        await decide()
    assert (
        client.database["users"].documents[str(target.id)]
        == before["users"][str(target.id)]
    )
    assert (
        client.database["access_requests"].documents[str(request.id)]
        == before["access_requests"][str(request.id)]
    )
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    auth.authorize = original_authorize
    client.database["audit_events"].fail_insert = True
    with pytest.raises(Exception):
        await decide()
    assert state(client) == before
    client.database["audit_events"].fail_insert = False
    expected = version(client)
    results = await asyncio.gather(
        decide(expected_policy_version=expected),
        decide(expected_policy_version=expected),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    result = next(item for item in results if not isinstance(item, Exception))
    assert result.approval_status == "approved"
    assert (
        client.database["access_requests"].documents[str(request.id)]["status"]
        == "approved"
    )
    other = user_record(provider_account_id="other", email_key="other@example.test")
    other_request = access_request(other)
    client.database["users"].documents[str(other.id)] = other.document()
    client.database["access_requests"].documents[str(other_request.id)] = (
        other_request.document()
    )
    auth.decision.policy_version = version(client)
    rejected = await service.decide(
        principal,
        other_request.id,
        approve=False,
        role_id=None,
        scope=(),
        expected_policy_version=version(client),
        audit_request_id=uuid4(),
    )
    assert rejected.approval_status == "rejected"
    assert (
        client.database["access_requests"].documents[str(other_request.id)]["status"]
        == "rejected"
    )
