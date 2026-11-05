"""Signed route tests use synthetic records and a transaction substitute."""

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.admission import AdmissionController
from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.own_access import OwnAccessService
from goodshowroom_ai.storage.mongo import AppDatabase
from tests.test_identity_bridge import _mint, _request_claims, _verifier
from tests.test_identity_storage import (
    NOW,
    login_record,
    user_record,
    role_record,
    selected_grant,
)
from tests.test_access_request_storage import access_request
from tests.test_authorization import sites_setting
from tests.workflow_fake import RollbackClient


@pytest.mark.asyncio
async def test_signed_pending_status_request_duplicates_and_limits(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    user = user_record(approval_status="pending", role_id=None, scope_limit=())
    login = login_record(user.id)
    client.database["users"].documents[str(user.id)] = user.document()
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    app = create_app(Settings(), own_access=service)

    def token(method, path, **changes):
        return _mint(
            signing_key,
            _request_claims(
                sub=str(user.id),
                login_session_id=str(login.id),
                generation=login.generation,
                method=method,
                path=path,
                digest=operation_digest(method, path),
                **changes,
            ),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        me_token = token("GET", "/v1/me")
        request_token = token("POST", "/v1/access-requests")
        me = await http.get("/v1/me", headers={"authorization": f"Bearer {me_token}"})
        assert me.status_code == 200
        assert me.json()["data"] == {
            "access_status": "pending",
            "role": None,
            "capabilities": [],
            "scope_choices": [],
        }
        assert me.headers["cache-control"] == "no-store"
        first = await http.post(
            "/v1/access-requests", headers={"authorization": f"Bearer {request_token}"}
        )
        assert first.status_code == 201
        again = await http.post(
            "/v1/access-requests", headers={"authorization": f"Bearer {request_token}"}
        )
        assert again.status_code == 200 and first.json()["data"] == again.json()["data"]
        assert len(client.database["access_requests"].documents) == 1
        assert (
            await http.post(
                "/v1/access-requests",
                headers={"authorization": f"Bearer {request_token}"},
                json={"role": "admin"},
            )
        ).status_code in (401, 403, 422)
        assert (
            await http.post(
                "/v1/access-requests",
                headers={"authorization": f"Bearer {request_token}"},
                json={"user_id": str(uuid4())},
            )
        ).status_code in (401, 403, 422)
        assert (
            await http.get(
                "/v1/me",
                headers=[
                    ("authorization", "Bearer " + me_token),
                    ("authorization", "Bearer " + me_token),
                ],
            )
        ).status_code in (401, 403)
        assert (
            await http.get(
                "/v1/me", headers={"authorization": "Bearer " + me_token + "invalid"}
            )
        ).status_code in (401, 403)
        assert (await http.get("/v1/me")).status_code in (401, 403)
        assert (
            await http.get(
                "/v1/me", headers={"authorization": f"Bearer {request_token}"}
            )
        ).status_code in (401, 403)


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_return_same_record(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    user = user_record(approval_status="pending", role_id=None, scope_limit=())
    login = login_record(user.id)
    client.database["users"].documents[str(user.id)] = user.document()
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    app = create_app(Settings(), own_access=service)
    path = "/v1/access-requests"
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(user.id),
            login_session_id=str(login.id),
            generation=login.generation,
            method="POST",
            path=path,
            digest=operation_digest("POST", path),
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        responses = await asyncio.gather(
            *(
                http.post(path, headers={"authorization": "Bearer " + token})
                for _ in range(2)
            )
        )
        assert sorted(response.status_code for response in responses) == [200, 201]
        assert responses[0].json()["data"] == responses[1].json()["data"]
        assert len(client.database["access_requests"].documents) == 1
        for _ in range(8):
            assert (
                await http.post(
                    "/v1/access-requests",
                    headers={"authorization": f"Bearer {token}"},
                )
            ).status_code == 200
        assert (
            await http.post(
                "/v1/access-requests",
                headers={"authorization": f"Bearer {token}"},
            )
        ).status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "revoked"])
async def test_inactive_user_can_only_read_own_status(signing_key, status):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    user = user_record(approval_status=status, role_id=None, scope_limit=())
    login = login_record(user.id)
    client.database["users"].documents[str(user.id)] = user.document()
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    app = create_app(Settings(), own_access=service)

    def token(method, path):
        return _mint(
            signing_key,
            _request_claims(
                sub=str(user.id),
                login_session_id=str(login.id),
                generation=login.generation,
                method=method,
                path=path,
                digest=operation_digest(method, path),
            ),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        me = await http.get(
            "/v1/me", headers={"authorization": "Bearer " + token("GET", "/v1/me")}
        )
        assert me.status_code == 200 and me.json()["data"]["access_status"] == status
        assert me.json()["data"]["scope_choices"] == []
        request = await http.post(
            "/v1/access-requests",
            headers={"authorization": "Bearer " + token("POST", "/v1/access-requests")},
        )
        assert request.status_code == 403
        assert not client.database["access_requests"].documents


@pytest.mark.asyncio
async def test_approved_status_requires_current_active_role_and_scope(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    role = role_record(
        active=True, grants=(selected_grant(),), capabilities=frozenset({"chat"})
    )
    user = user_record(
        approval_status="approved", role_id=role.id, scope_limit=(selected_grant(),)
    )
    login = login_record(user.id)
    for name, record in (("users", user), ("roles", role), ("login_sessions", login)):
        client.database[name].documents[str(record.id)] = record.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    app = create_app(Settings(), own_access=service)
    assertion = _mint(
        signing_key,
        _request_claims(
            sub=str(user.id),
            login_session_id=str(login.id),
            generation=login.generation,
            method="GET",
            path="/v1/me",
            digest=operation_digest("GET", "/v1/me"),
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        response = await http.get(
            "/v1/me", headers={"authorization": "Bearer " + assertion}
        )
        assert response.status_code == 200
        assert response.json()["data"]["role"] == role.name_key
        assert response.json()["data"]["scope_choices"] == []  # No configured sites.
        sites = sites_setting("site_a", "ungranted_site")
        client.database["app_settings"].documents["sites"] = sites.document()
        response = await http.get(
            "/v1/me", headers={"authorization": "Bearer " + assertion}
        )
        assert response.status_code == 200
        assert response.json()["data"]["scope_choices"][0]["sites"] == ["site_a"]
        assert "customer-42" not in response.text
        client.database["roles"].documents[str(role.id)] = role.model_copy(
            update={"active": False}
        ).document()
        assert (
            await http.get("/v1/me", headers={"authorization": "Bearer " + assertion})
        ).status_code == 403


@pytest.mark.asyncio
async def test_user_change_between_status_checks_blocks_release(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    user = user_record(approval_status="pending", role_id=None, scope_limit=())
    login = login_record(user.id)
    client.database["users"].documents[str(user.id)] = user.document()
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    original = service._snapshot
    calls = 0

    async def changing(*args):
        nonlocal calls
        result = await original(*args)
        calls += 1
        if calls == 1:
            client.database["users"].documents[str(user.id)] = user.model_copy(
                update={"approval_status": "revoked"}
            ).document()
        return result

    service._snapshot = changing
    app = create_app(Settings(), own_access=service)
    token = _mint(
        signing_key,
        _request_claims(
            sub=str(user.id),
            login_session_id=str(login.id),
            generation=login.generation,
            method="GET",
            path="/v1/me",
            digest=operation_digest("GET", "/v1/me"),
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        result = await http.get("/v1/me", headers={"authorization": "Bearer " + token})
        assert result.status_code == 403
        assert "data" not in result.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status", ["rejected", "revoked"])
async def test_closed_history_prevents_resubmission_and_audit_failure_rolls_back(
    signing_key,
    previous_status,
):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    user = user_record(approval_status="pending", role_id=None, scope_limit=())
    login = login_record(user.id)
    client.database["users"].documents[str(user.id)] = user.document()
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    app = create_app(Settings(), own_access=service)
    path = "/v1/access-requests"
    assertion = _mint(
        signing_key,
        _request_claims(
            sub=str(user.id),
            login_session_id=str(login.id),
            generation=login.generation,
            method="POST",
            path=path,
            digest=operation_digest("POST", path),
        ),
    )
    headers = {"authorization": "Bearer " + assertion}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        client.database["audit_events"].fail_insert = True
        assert (await http.post(path, headers=headers)).status_code == 503
        assert not client.database["access_requests"].documents
        client.database["audit_events"].fail_insert = False
        previous = access_request(user).model_copy(
            update={
                "active": False,
                "status": previous_status,
                "reviewed_by": uuid4(),
                "reviewed_at": NOW,
            }
        )
        client.database["access_requests"].documents[str(previous.id)] = (
            previous.document()
        )
        assert (await http.post(path, headers=headers)).status_code == 409
        assert len(client.database["access_requests"].documents) == 1


@pytest.mark.asyncio
async def test_spoofed_subject_and_revoked_login_cannot_read_status(signing_key):
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    owner = user_record(approval_status="pending", role_id=None, scope_limit=())
    other = user_record(
        approval_status="pending",
        role_id=None,
        scope_limit=(),
        email_key="other@example.test",
    )
    login = login_record(owner.id)
    for user in (owner, other):
        client.database["users"].documents[str(user.id)] = user.document()
    client.database["login_sessions"].documents[str(login.id)] = login.document()
    service = OwnAccessService(
        db, _verifier(signing_key), AdmissionController(), lambda: NOW
    )
    app = create_app(Settings(), own_access=service)

    def assertion(user):
        return _mint(
            signing_key,
            _request_claims(
                sub=str(user.id),
                login_session_id=str(login.id),
                generation=login.generation,
                method="GET",
                path="/v1/me",
                digest=operation_digest("GET", "/v1/me"),
            ),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        assert (
            await http.get(
                "/v1/me", headers={"authorization": "Bearer " + assertion(other)}
            )
        ).status_code in (401, 403)
        client.database["login_sessions"].documents[str(login.id)] = login.model_copy(
            update={"revoked_at": NOW}
        ).document()
        assert (
            await http.get(
                "/v1/me", headers={"authorization": "Bearer " + assertion(owner)}
            )
        ).status_code in (401, 403)
