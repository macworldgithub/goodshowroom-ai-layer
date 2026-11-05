"""Synthetic signed HTTP checks for login lease and logout behavior."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.app import create_app
from goodshowroom_ai.authorization import Principal
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.login_lifecycle import LoginLifecycle
from goodshowroom_ai.storage.mongo import AppDatabase
from goodshowroom_ai.temporary import TemporaryMemoryStore, TemporaryUnavailable
from tests.test_temporary import create_context
from tests.test_access_workflow import Authorization, version
from tests.test_identity_bridge import (
    _bootstrap_claims,
    _mint,
    _request_claims,
    _verifier,
)
from tests.test_identity_storage import NOW, role_record, selected_grant
from tests.workflow_fake import RollbackClient


@pytest.mark.asyncio
async def test_signed_lifecycle_route_lease_replay_and_cleanup(signing_key):
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
    now = [NOW]
    verifier = _verifier(signing_key)
    exchange = AccessWorkflow(
        db,
        verifier,
        Authorization(),
        lambda: now[0],
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    exchange_path = "/v1/internal/login/exchange"
    bootstrap = _bootstrap_claims(
        path=exchange_path,
        digest=operation_digest("POST", exchange_path),
        email="admin@example.test",
    )
    user, login = await exchange.exchange(
        _mint(signing_key, bootstrap),
        expected_policy_version=version(client),
        request_id=uuid4(),
    )
    memory = type("Memory", (), {})()
    memory.refresh_login = AsyncMock()
    memory.invalidate_login = AsyncMock()
    lifecycle = LoginLifecycle(db, verifier, memory, lambda: now[0])
    app = create_app(Settings(), login_lifecycle=lifecycle)

    def assertion(path, issued=NOW):
        stamp = int(issued.timestamp())
        return _mint(
            signing_key,
            _request_claims(
                sub=str(user.id),
                login_session_id=str(login.id),
                generation=login.generation,
                path=path,
                digest=operation_digest("POST", path),
                iat=stamp,
                nbf=stamp,
                exp=stamp + 120,
            ),
        )

    renew_path = "/v1/internal/login/renew"
    revoke_path = "/v1/internal/login/revoke"
    renew_token, revoke_token = (
        assertion(renew_path, NOW + timedelta(seconds=30)),
        assertion(revoke_path),
    )
    memory.invalidate_login.side_effect = [
        RuntimeError("synthetic cleanup failure"),
        None,
        None,
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        now[0] = NOW + timedelta(minutes=5, seconds=1)
        stamp = int(now[0].timestamp())
        late = _mint(
            signing_key,
            _request_claims(
                sub=str(user.id),
                login_session_id=str(login.id),
                generation=login.generation,
                path=renew_path,
                digest=operation_digest("POST", renew_path),
                iat=stamp,
                nbf=stamp,
                exp=stamp + 120,
            ),
        )
        expired = await http.post(
            renew_path, headers={"authorization": f"Bearer {late}"}
        )
        assert expired.status_code in {401, 403}
        assert memory.refresh_login.await_count == 0
        now[0] = NOW + timedelta(seconds=30)
        invalid = await http.post(
            renew_path, headers={"authorization": f"Bearer {revoke_token}"}
        )
        assert invalid.status_code in {401, 403}
        renewed = await http.post(
            renew_path, headers={"authorization": f"Bearer {renew_token}"}
        )
        assert renewed.status_code == 200
        assert (
            renewed.json()["data"]["lease_valid_until"]
            == (now[0] + timedelta(minutes=5)).isoformat()
        )
        assert memory.refresh_login.await_count == 1
        now[0] += timedelta(seconds=10)
        replay = await http.post(
            renew_path, headers={"authorization": f"Bearer {renew_token}"}
        )
        assert replay.status_code == 200
        assert (
            replay.json()["data"]["lease_valid_until"]
            == renewed.json()["data"]["lease_valid_until"]
        )
        logout = await http.post(
            revoke_path, headers={"authorization": f"Bearer {revoke_token}"}
        )
        assert logout.status_code == 500
        assert (
            client.database["login_sessions"].documents[str(login.id)]["revoked_at"]
            == now[0]
        )
        assert memory.invalidate_login.await_count == 1
        again = await http.post(
            revoke_path, headers={"authorization": f"Bearer {revoke_token}"}
        )
        assert again.status_code == 204
        assert memory.invalidate_login.await_count == 2
        again = await http.post(
            revoke_path, headers={"authorization": f"Bearer {revoke_token}"}
        )
        assert again.status_code == 204
        denied = await http.post(
            renew_path, headers={"authorization": f"Bearer {renew_token}"}
        )
        assert denied.status_code in {401, 403}


@pytest.mark.asyncio
async def test_signed_logout_clears_real_temporary_memory_and_cancels_work(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    await db.initialize_identity_indexes()
    await db.initialize_access_request_indexes()
    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    client.database["roles"].documents[str(role.id)] = role.document()
    verifier = _verifier(signing_key)
    workflow = AccessWorkflow(
        db,
        verifier,
        Authorization(),
        lambda: NOW,
        "admin@example.test",
        role.id,
        (selected_grant(),),
    )
    path = "/v1/internal/login/exchange"
    user, login = await workflow.exchange(
        _mint(
            signing_key,
            _bootstrap_claims(
                path=path,
                digest=operation_digest("POST", path),
                email="admin@example.test",
            ),
        ),
        expected_policy_version=version(client),
        request_id=uuid4(),
    )
    principal = Principal(
        user_id=user.id, login_id=login.id, generation=login.generation
    )
    authorization = SimpleNamespace(
        authorize=AsyncMock(
            return_value=SimpleNamespace(snapshot=SimpleNamespace(login=login))
        )
    )
    memory = TemporaryMemoryStore(authorization=authorization, clock=lambda: NOW)
    _, context, command = await create_context(memory, principal)
    entered = asyncio.Event()

    async def active_work():
        await memory.begin_turn(principal, context, command)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(active_work())
    await entered.wait()
    path = "/v1/internal/login/revoke"
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(user.id),
            login_session_id=str(login.id),
            generation=login.generation,
            path=path,
            digest=operation_digest("POST", path),
        ),
    )
    app = create_app(
        Settings(), login_lifecycle=LoginLifecycle(db, verifier, memory, lambda: NOW)
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test.invalid"
        ) as http:
            result = await http.post(path, headers={"authorization": f"Bearer {token}"})
        assert result.status_code == 204
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(TemporaryUnavailable):
            await memory.begin_turn(principal, context, command)
        assert (
            client.database["login_sessions"].documents[str(login.id)]["revoked_at"]
            == NOW
        )
    finally:
        task.cancel()
        await memory.close()
