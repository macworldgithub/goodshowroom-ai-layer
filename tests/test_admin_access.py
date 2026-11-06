"""Administrator identity is bound to the full signed operation."""

from types import SimpleNamespace
from uuid import uuid4
import json
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.admin_access import (
    AdminAccess,
    RecordHandlePosition,
    _RecordHandlePayload,
)
from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.app import create_app
from goodshowroom_ai.api_contracts import AdminScopeGrant
from goodshowroom_ai.authorization import (
    AuthorizationService,
    PolicySnapshot,
    AccessDenied,
)
from goodshowroom_ai.config import Settings
from goodshowroom_ai.cursors import CursorCodec, CursorRejected
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.storage.mongo import AppDatabase
from tests.test_identity_bridge import _mint, _request_claims, _verifier
from tests.test_identity_storage import NOW, login_record, user_record
from tests.workflow_fake import RollbackClient
from tests.test_access_management import policy_version, refresh_decision, store
from tests.test_access_request_storage import access_request

pytest_plugins = ("tests.test_access_management",)


@pytest.mark.asyncio
async def test_admin_authorization_binds_body_and_current_permission(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_args, **_kwargs: client
    )
    user = user_record()
    login = login_record(user.id)
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    path = "/v1/admin/preapprovals"
    body = b'{"expected_policy_version":0}'
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(user.id),
            login_session_id=str(login.id),
            generation=login.generation,
            method="POST",
            path=path,
            digest=operation_digest("POST", path, body=body),
        ),
    )

    class Authorization:
        async def authorize(self, principal, action):
            assert principal.user_id == user.id
            assert action.action == "manage_access"
            return SimpleNamespace(policy_version=0)

    service = AdminAccess(db, _verifier(signing_key), Authorization(), lambda: NOW)
    principal, decision = await service.authorize(
        token, method="POST", path=path, body=body
    )
    assert principal.user_id == user.id and decision.policy_version == 0
    with pytest.raises(PermissionError):
        await service.authorize(token, method="POST", path=path, body=body + b" ")


@pytest.mark.asyncio
async def test_real_policy_service_denies_a_role_without_management(env, signing_key):
    role = env.role.model_copy(update={"capabilities": frozenset({"chat"})})
    snapshot = PolicySnapshot(
        policy_version=policy_version(env.client),
        user=env.actor,
        role=role,
        login=env.login,
    )

    async def policy(_principal):
        return snapshot

    async def current(_principal):
        return True

    authorization = AuthorizationService(
        load_policy=policy, check_identity=current, clock=lambda: NOW
    )
    service = AdminAccess(env.db, _verifier(signing_key), authorization, lambda: NOW)
    path = "/v1/admin/access-requests"
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(env.actor.id),
            login_session_id=str(env.login.id),
            generation=env.login.generation,
            method="GET",
            path=path,
            digest=operation_digest("GET", path),
        ),
    )
    with pytest.raises(AccessDenied):
        await service.authorize(token, method="GET", path=path)


def test_selected_record_handles_are_actor_and_policy_bound(env):
    codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)
    service = AdminAccess(env.db, None, env.auth, lambda: NOW)
    source = env.actor.scope_limit[0].selected_records[0]
    version = policy_version(env.client)
    raw = env.actor.scope_limit[0].model_dump(mode="json")

    def handle(actor, policy):
        position = RecordHandlePosition(
            actor=actor, policy_version=policy, source=source
        )
        return codec._encode_chat_position(position, _RecordHandlePayload)

    raw["selected_records"] = [handle(env.actor.id, version)]
    item = AdminScopeGrant.model_validate_json(json.dumps(raw))
    assert (
        service._resolve_scope((item,), env.principal, version, codec)
        == env.actor.scope_limit
    )
    for wrong in (
        handle(uuid4(), version),
        handle(env.actor.id, version + 1),
        "forged",
    ):
        raw["selected_records"] = [wrong]
        item = AdminScopeGrant.model_validate_json(json.dumps(raw))
        with pytest.raises(CursorRejected):
            service._resolve_scope((item,), env.principal, version, codec)


@pytest.mark.asyncio
async def test_handle_issuance_requires_an_existing_selected_record(env):
    codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)
    service = AdminAccess(env.db, None, env.auth, lambda: NOW)
    source = env.actor.scope_limit[0].selected_records[0]
    handle = await service.issue_known_record_handle(env.principal, source, codec)
    assert codec._decode_chat_position(handle, _RecordHandlePayload).source == source
    with pytest.raises(CursorRejected):
        await service.issue_known_record_handle(
            env.principal, source.model_copy(update={"key_value": "other"}), codec
        )


@pytest.mark.asyncio
async def test_preapproval_route_requires_signed_body_and_atomic_audit(
    env, signing_key
):
    service = AdminAccess(env.db, _verifier(signing_key), env.auth, lambda: NOW)
    app = create_app(Settings(), admin_access=service)
    app.state.cursor_codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)

    def scope_with_handle():
        scope = env.actor.scope_limit[0].model_dump(mode="json")
        position = RecordHandlePosition(
            actor=env.actor.id,
            policy_version=policy_version(env.client),
            source=env.actor.scope_limit[0].selected_records[0],
        )
        scope["selected_records"] = [
            app.state.cursor_codec._encode_chat_position(position, _RecordHandlePayload)
        ]
        return scope

    path = "/v1/admin/preapprovals"
    data = {
        "email": "new@example.test",
        "role_id": str(env.role.id),
        "scope_limit": [scope_with_handle()],
        "expires_at": None,
        "expected_policy_version": policy_version(env.client),
    }
    body = json.dumps(data, separators=(",", ":")).encode()

    def token(raw):
        return _mint(
            signing_key,
            _request_claims(
                sub=str(env.actor.id),
                login_session_id=str(env.login.id),
                generation=env.login.generation,
                method="POST",
                path=path,
                digest=operation_digest("POST", path, body=raw),
            ),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        denied = await http.post(
            path,
            content=body + b" ",
            headers={"authorization": "Bearer " + token(body)},
        )
        assert denied.status_code in {401, 403}
        assert not env.client.database["access_requests"].documents
        env.client.database["audit_events"].fail_insert = True
        failure = await http.post(
            path, content=body, headers={"authorization": "Bearer " + token(body)}
        )
        assert failure.status_code == 503
        assert not env.client.database["access_requests"].documents
        env.client.database["audit_events"].fail_insert = False
        result = await http.post(
            path, content=body, headers={"authorization": "Bearer " + token(body)}
        )
        assert result.status_code == 201
        assert result.json()["data"]["status"] == "pending"
        assert len(env.client.database["access_requests"].documents) == 1
        assert len(env.client.database["audit_events"].documents) == 1
        refresh_decision(env)
        data["scope_limit"] = [scope_with_handle()]
        data["expected_policy_version"] = policy_version(env.client)
        data["expires_at"] = (NOW + timedelta(days=1)).isoformat()
        changed = json.dumps(data, separators=(",", ":")).encode()
        conflict = await http.post(
            path, content=changed, headers={"authorization": "Bearer " + token(changed)}
        )
        assert conflict.status_code == 409
        assert len(env.client.database["access_requests"].documents) == 1
        identifier = result.json()["data"]["id"]
        revoke_path = f"/v1/admin/preapprovals/{identifier}/revoke"
        revoke_body = json.dumps(
            {"expected_policy_version": policy_version(env.client)},
            separators=(",", ":"),
        ).encode()
        revoke_token = _mint(
            signing_key,
            _request_claims(
                sub=str(env.actor.id),
                login_session_id=str(env.login.id),
                generation=env.login.generation,
                method="POST",
                path=revoke_path,
                digest=operation_digest("POST", revoke_path, body=revoke_body),
            ),
        )
        revoked = await http.post(
            revoke_path,
            content=revoke_body,
            headers={"authorization": "Bearer " + revoke_token},
        )
        assert revoked.status_code == 200
        assert revoked.json()["data"]["status"] == "revoked"


@pytest.mark.asyncio
async def test_request_list_pages_bind_actor_filter_and_current_policy(
    env, signing_key
):
    for index in range(2):
        user = user_record(
            email_key=f"pending{index}@example.test",
            provider_account_id=f"pending-{index}",
        )
        store(env.client.database["users"], user)
        store(env.client.database["access_requests"], access_request(user))
    service = AdminAccess(env.db, _verifier(signing_key), env.auth, lambda: NOW)
    app = create_app(Settings(), admin_access=service)
    app.state.cursor_codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)
    path = "/v1/admin/access-requests"

    def token(query):
        return _mint(
            signing_key,
            _request_claims(
                sub=str(env.actor.id),
                login_session_id=str(env.login.id),
                generation=env.login.generation,
                method="GET",
                path=path,
                digest=operation_digest("GET", path, query=query.encode()),
            ),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        query = "status=pending&page_size=1"
        first = await http.get(
            path + "?" + query, headers={"authorization": "Bearer " + token(query)}
        )
        assert first.status_code == 200
        assert len(first.json()["data"]["items"]) == 1
        cursor = first.json()["data"]["next_cursor"]
        assert cursor
        query2 = query + "&cursor=" + cursor
        second = await http.get(
            path + "?" + query2, headers={"authorization": "Bearer " + token(query2)}
        )
        assert second.status_code == 200
        assert (
            second.json()["data"]["items"][0]["id"]
            != first.json()["data"]["items"][0]["id"]
        )
        env.auth.decision.policy_version += 1
        stale = await http.get(
            path + "?" + query2, headers={"authorization": "Bearer " + token(query2)}
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_decision_route_rejects_missing_permission_and_stale_policy(
    env, signing_key
):
    target = user_record(email_key="target@example.test", provider_account_id="target")
    store(env.client.database["users"], target)
    record = store(env.client.database["access_requests"], access_request(target))
    verifier = _verifier(signing_key)
    workflow = AccessWorkflow(
        env.db,
        verifier,
        env.auth,
        lambda: NOW,
        "admin@example.test",
        env.role.id,
        env.actor.scope_limit,
    )
    service = AdminAccess(env.db, verifier, env.auth, lambda: NOW, workflow)
    app = create_app(Settings(), admin_access=service)
    path = f"/v1/admin/access-requests/{record.id}/decision"
    body = json.dumps(
        {
            "decision": "reject",
            "role_id": None,
            "scope_limit": [],
            "expected_policy_version": policy_version(env.client),
        },
        separators=(",", ":"),
    ).encode()
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(env.actor.id),
            login_session_id=str(env.login.id),
            generation=env.login.generation,
            method="POST",
            path=path,
            digest=operation_digest("POST", path, body=body),
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        env.auth.allowed = False
        denied = await http.post(
            path, content=body, headers={"authorization": "Bearer " + token}
        )
        assert denied.status_code == 403
        assert (
            env.client.database["access_requests"].documents[str(record.id)]["status"]
            == "pending"
        )
        env.auth.allowed = True
        env.auth.decision.policy_version += 1
        stale = await http.post(
            path, content=body, headers={"authorization": "Bearer " + token}
        )
        assert stale.status_code == 409
        refresh_decision(env)
        result = await http.post(
            path, content=body, headers={"authorization": "Bearer " + token}
        )
        assert result.status_code == 200
        assert result.json()["data"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_admin_route_limits_authenticated_requests(env, signing_key):
    service = AdminAccess(env.db, _verifier(signing_key), env.auth, lambda: NOW)
    app = create_app(Settings(), admin_access=service)
    path = "/v1/admin/preapprovals"
    body = b"{}"
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(env.actor.id),
            login_session_id=str(env.login.id),
            generation=env.login.generation,
            method="POST",
            path=path,
            digest=operation_digest("POST", path, body=body),
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        for _ in range(10):
            result = await http.post(
                path, content=body, headers={"authorization": "Bearer " + token}
            )
            assert result.status_code == 422
        result = await http.post(
            path, content=body, headers={"authorization": "Bearer " + token}
        )
        assert result.status_code == 429


@pytest.mark.asyncio
async def test_approval_route_accepts_only_bound_selected_record_handles(
    env, signing_key
):
    target = user_record(
        email_key="approve@example.test", provider_account_id="approve"
    )
    store(env.client.database["users"], target)
    record = store(env.client.database["access_requests"], access_request(target))
    verifier = _verifier(signing_key)
    workflow = AccessWorkflow(
        env.db,
        verifier,
        env.auth,
        lambda: NOW,
        "admin@example.test",
        env.role.id,
        env.actor.scope_limit,
    )
    app = create_app(
        Settings(),
        admin_access=AdminAccess(env.db, verifier, env.auth, lambda: NOW, workflow),
    )
    app.state.cursor_codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)
    path = f"/v1/admin/access-requests/{record.id}/decision"
    scope = env.actor.scope_limit[0].model_dump(mode="json")
    position = RecordHandlePosition(
        actor=env.actor.id,
        policy_version=policy_version(env.client),
        source=env.actor.scope_limit[0].selected_records[0],
    )
    handle = app.state.cursor_codec._encode_chat_position(
        position, _RecordHandlePayload
    )
    data = {
        "decision": "approve",
        "role_id": str(env.role.id),
        "scope_limit": [scope],
        "expected_policy_version": policy_version(env.client),
    }

    async def send(http):
        body = json.dumps(data, separators=(",", ":")).encode()
        token = _mint(
            signing_key,
            _request_claims(
                sub=str(env.actor.id),
                login_session_id=str(env.login.id),
                generation=env.login.generation,
                method="POST",
                path=path,
                digest=operation_digest("POST", path, body=body),
            ),
        )
        return await http.post(
            path, content=body, headers={"authorization": "Bearer " + token}
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        assert (await send(http)).status_code == 422
        assert (
            env.client.database["users"].documents[str(target.id)]["approval_status"]
            == "pending"
        )
        scope["selected_records"] = [handle]
        result = await send(http)
        assert result.status_code == 200
        assert result.json()["data"]["status"] == "approved"
        assert (
            env.client.database["users"].documents[str(target.id)]["approval_status"]
            == "approved"
        )
