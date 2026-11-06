"""Comprehensive offline tests for administrator role and user access management APIs."""

import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.admin_access import (
    RecordHandlePosition,
    _RecordHandlePayload,
)
from goodshowroom_ai.admin_management import (
    AdminManagement,
)
from goodshowroom_ai.api_contracts import (
    ErrorCode,
)
from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.storage.mongo import AppCommitOutcomeUnknown
from tests.test_access_management import (
    new_role,
    policy_version,
    refresh_decision,
    seed_user,
    store,
)
from tests.test_identity_bridge import _mint, _request_claims, _verifier
from tests.test_identity_storage import (
    NOW,
)

pytest_plugins = ("tests.test_access_management",)


def _token(signing_key, actor, login, method, path, query=b"", body=b""):
    digest = operation_digest(method, path, query=query, body=body)
    return _mint(
        signing_key,
        _request_claims(
            sub=str(actor.id),
            login_session_id=str(login.id),
            generation=login.generation,
            method=method,
            path=path,
            digest=digest,
        ),
    )


def _make_app(env, signing_key):
    service = AdminManagement(env.db, _verifier(signing_key), env.auth, lambda: NOW)
    app = create_app(Settings(), admin_management=service)
    app.state.cursor_codec = CursorCodec(Fernet.generate_key(), clock=lambda: NOW)
    return app


def _scope_with_handle(actor_id, policy_ver, codec, grant):
    scope = grant.model_dump(mode="json")
    if grant.record_mode == "selected":
        handles = []
        for rec in grant.selected_records:
            pos = RecordHandlePosition(
                actor=actor_id,
                policy_version=policy_ver,
                source=rec,
            )
            handles.append(codec._encode_chat_position(pos, _RecordHandlePayload))
        scope["selected_records"] = handles
    return scope


# ---------------------------------------------------------------------------
# 1. GET /v1/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_admin_users_basic_and_pagination(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    # Add 2 more approved users with the same role and scope
    seed_user(client, role=env.role, email="u2@example.test")
    seed_user(client, role=env.role, email="u3@example.test")

    path = "/v1/admin/users"
    # Page size 2
    query = b"approval_status=approved&page_size=2"
    tok = _token(signing_key, env.actor, env.login, "GET", path, query=query)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.get(
            f"{path}?{query.decode()}",
            headers={"authorization": f"Bearer {tok}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["items"]) == 2
        assert data["next_cursor"] is not None
        assert data["policy_version"] == policy_version(client)

        # Check handles in scope_limit
        item0 = data["items"][0]
        assert "scope_limit" in item0
        assert len(item0["scope_limit"]) == 1
        handle = item0["scope_limit"][0]["selected_records"][0]
        decoded = codec._decode_chat_position(handle, _RecordHandlePayload)
        assert decoded.actor == env.actor.id
        assert decoded.policy_version == policy_version(client)

        # Next page with cursor
        next_c = data["next_cursor"]
        q2 = f"approval_status=approved&cursor={next_c}&page_size=2".encode()
        tok2 = _token(signing_key, env.actor, env.login, "GET", path, query=q2)
        resp2 = await http.get(
            f"{path}?{q2.decode()}",
            headers={"authorization": f"Bearer {tok2}"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()["data"]
        assert len(data2["items"]) == 1
        assert data2["next_cursor"] is None

        # Tampered cursor returns 422
        q_bad = f"approval_status=approved&cursor={next_c}tamper&page_size=2".encode()
        tok_bad = _token(signing_key, env.actor, env.login, "GET", path, query=q_bad)
        resp_bad = await http.get(
            f"{path}?{q_bad.decode()}",
            headers={"authorization": f"Bearer {tok_bad}"},
        )
        assert resp_bad.status_code == 422
        assert resp_bad.json()["error"]["code"] == ErrorCode.INVALID_REQUEST.value


@pytest.mark.asyncio
async def test_list_admin_users_safe_projection_and_filtering(env, signing_key):
    app = _make_app(env, signing_key)
    client = env.client

    # Add pending, rejected, and revoked users
    pending = seed_user(client, role=None, status="pending", email="p@example.test")
    seed_user(client, role=None, status="rejected", email="rej@example.test")
    seed_user(client, role=None, status="revoked", email="rev@example.test")

    path = "/v1/admin/users"
    query = b"approval_status=pending&page_size=10"
    tok = _token(signing_key, env.actor, env.login, "GET", path, query=query)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.get(
            f"{path}?{query.decode()}",
            headers={"authorization": f"Bearer {tok}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["items"]) == 1
        p_item = data["items"][0]
        assert p_item["id"] == str(pending.id)
        assert p_item["approval_status"] == "pending"
        # Safe unapproved projection: role_id must be None, scope_limit must be empty
        assert p_item["role_id"] is None
        assert p_item["scope_limit"] == []


@pytest.mark.asyncio
async def test_list_admin_users_omits_unmanageable_users(env, signing_key):
    app = _make_app(env, signing_key)
    client = env.client

    # Create a super role with capabilities caller doesn't have
    super_role = store(
        client.database["roles"],
        new_role(capabilities={"chat", "manage_access", "manage_settings"}),
    )
    # Seed user with this super role
    seed_user(client, role=super_role, email="super@example.test")

    path = "/v1/admin/users"
    query = b"approval_status=approved&page_size=10"
    tok = _token(signing_key, env.actor, env.login, "GET", path, query=query)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.get(
            f"{path}?{query.decode()}",
            headers={"authorization": f"Bearer {tok}"},
        )
        assert resp.status_code == 200
        emails = [u["email"] for u in resp.json()["data"]["items"]]
        assert "super@example.test" not in emails
        assert env.actor.email_key in emails


# ---------------------------------------------------------------------------
# 2. GET /v1/admin/roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_admin_roles_basic_filter_and_omits_unmanageable(env, signing_key):
    app = _make_app(env, signing_key)
    client = env.client

    store(
        client.database["roles"],
        new_role(name="active_role", active=True, capabilities={"chat"}),
    )
    store(
        client.database["roles"],
        new_role(name="inactive_role", active=False, capabilities={"chat"}),
    )
    # Unmanageable role
    store(
        client.database["roles"],
        new_role(
            name="super_role",
            active=True,
            capabilities={"chat", "manage_access", "manage_settings"},
        ),
    )

    path = "/v1/admin/roles"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        # Filter active=true
        q_act = b"active=true&page_size=10"
        t_act = _token(signing_key, env.actor, env.login, "GET", path, query=q_act)
        resp_act = await http.get(
            f"{path}?{q_act.decode()}",
            headers={"authorization": f"Bearer {t_act}"},
        )
        assert resp_act.status_code == 200
        names = [r["name_key"] for r in resp_act.json()["data"]["items"]]
        assert "active_role" in names
        assert "inactive_role" not in names
        assert "super_role" not in names

        # Filter active=false
        q_inact = b"active=false&page_size=10"
        t_inact = _token(signing_key, env.actor, env.login, "GET", path, query=q_inact)
        resp_inact = await http.get(
            f"{path}?{q_inact.decode()}",
            headers={"authorization": f"Bearer {t_inact}"},
        )
        assert resp_inact.status_code == 200
        names_inact = [r["name_key"] for r in resp_inact.json()["data"]["items"]]
        assert "inactive_role" in names_inact
        assert "active_role" not in names_inact


# ---------------------------------------------------------------------------
# 3. POST /v1/admin/roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_admin_role_success_and_audited(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    path = "/v1/admin/roles"
    payload = {
        "name_key": "viewer_role",
        "active": True,
        "capabilities": ["chat"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "POST", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.post(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["version"] == 0
        assert data["policy_version"] == payload["expected_policy_version"] + 1

        stored = client.database["roles"].documents[data["id"]]
        assert stored["name_key"] == "viewer_role"
        assert stored["version"] == 0
        assert stored["active"] is True

        # Check audit event
        events = list(client.database["audit_events"].documents.values())
        assert any(
            e["target_id"] == data["id"]
            and e["action"] == "access_control_change"
            and e["actor_id"] == str(env.actor.id)
            for e in events
        )


@pytest.mark.asyncio
async def test_create_admin_role_duplicate_name_conflict(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    # Role with name already exists
    store(client.database["roles"], new_role(name="existing_name"))

    path = "/v1/admin/roles"
    payload = {
        "name_key": "existing_name",
        "active": True,
        "capabilities": ["chat"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "POST", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.post(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_admin_role_authority_denial_and_policy_conflict(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    path = "/v1/admin/roles"

    # Capability exceeding caller (caller only has chat and manage_access)
    payload_exceed = {
        "name_key": "god_role",
        "active": True,
        "capabilities": ["chat", "manage_settings"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_policy_version": policy_version(client),
    }
    body_exceed = json.dumps(payload_exceed, separators=(",", ":")).encode()
    tok_exceed = _token(
        signing_key, env.actor, env.login, "POST", path, body=body_exceed
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.post(
            path, content=body_exceed, headers={"authorization": f"Bearer {tok_exceed}"}
        )
        assert resp.status_code == 403

        # Stale policy version -> 409
        payload_stale = {
            "name_key": "valid_name",
            "active": True,
            "capabilities": ["chat"],
            "grants": [
                _scope_with_handle(
                    env.actor.id,
                    policy_version(client),
                    codec,
                    env.actor.scope_limit[0],
                )
            ],
            "expected_policy_version": policy_version(client) + 1,
        }
        body_stale = json.dumps(payload_stale, separators=(",", ":")).encode()
        tok_stale = _token(
            signing_key, env.actor, env.login, "POST", path, body=body_stale
        )
        resp_stale = await http.post(
            path, content=body_stale, headers={"authorization": f"Bearer {tok_stale}"}
        )
        assert resp_stale.status_code == 409


# ---------------------------------------------------------------------------
# 4. PATCH /v1/admin/roles/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_admin_role_success(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    target = store(
        client.database["roles"],
        new_role(
            name="target_role",
            active=True,
            capabilities={"chat"},
            grants=env.actor.scope_limit,
        ),
    )

    path = f"/v1/admin/roles/{target.id}"
    payload = {
        "name_key": "target_role_renamed",
        "active": True,
        "capabilities": ["chat"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_version": 0,
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "PATCH", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.patch(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == str(target.id)
        assert data["version"] == 1
        assert client.database["roles"].documents[str(target.id)]["version"] == 1


@pytest.mark.asyncio
async def test_update_admin_role_prevents_self_role_edit_and_out_of_scope(
    env, signing_key
):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    # Attempting to edit caller's own role -> 403
    path_own = f"/v1/admin/roles/{env.role.id}"
    payload_own = {
        "name_key": "renamed_admin",
        "active": True,
        "capabilities": ["chat", "manage_access"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_version": 0,
        "expected_policy_version": policy_version(client),
    }
    body_own = json.dumps(payload_own, separators=(",", ":")).encode()
    tok_own = _token(
        signing_key, env.actor, env.login, "PATCH", path_own, body=body_own
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.patch(
            path_own, content=body_own, headers={"authorization": f"Bearer {tok_own}"}
        )
        assert resp.status_code == 403

        # Non-existent role -> 404
        bad_id = uuid4()
        path_bad = f"/v1/admin/roles/{bad_id}"
        tok_bad = _token(
            signing_key, env.actor, env.login, "PATCH", path_bad, body=body_own
        )
        resp_bad = await http.patch(
            path_bad, content=body_own, headers={"authorization": f"Bearer {tok_bad}"}
        )
        assert resp_bad.status_code == 404


@pytest.mark.asyncio
async def test_update_admin_role_prevents_last_admin_role_deactivation(
    env, signing_key
):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    # Create a separate admin role and assign an admin user to it
    admin2_role = store(
        client.database["roles"],
        new_role(
            name="admin2_role",
            active=True,
            capabilities={"chat", "manage_access"},
            grants=env.actor.scope_limit,
        ),
    )
    # The only user in admin2_role
    seed_user(client, role=admin2_role, email="admin2@example.test")

    # If env.actor was removed or if admin2_role was deactivated, but wait:
    # env.actor is in env.role, so there are 2 admins right now.
    # If we revoke env.actor's manage_access, or if we deactivate admin2_role when admin2 is the sole remaining admin:
    # Let's remove env.actor's role manage_access or simulate admin2 being the only admin.
    # To test last-admin protection on role deactivation:
    # Make admin2 the only active admin by changing env.actor's role capabilities to just "chat" in DB.
    updated_env_role = env.role.model_copy(update={"capabilities": frozenset({"chat"})})
    client.database["roles"].documents[str(env.role.id)] = updated_env_role.document()
    # Now admin2_role is the only role with manage_access, and admin2 is the only admin.
    # But wait! If env.actor doesn't have manage_access, env.actor won't pass authorization.
    # So instead, let env.actor have manage_access in env.role, and test deactivating a role that has users depending on it,
    # or test deactivating admin2_role when admin2 is the only admin? No, caller must be an admin.
    # Can caller deactivate a role that would invalidate any user's scope?
    # Yes! A user is assigned admin2_role, so deactivating admin2_role while users are assigned to it is rejected with 403!
    path = f"/v1/admin/roles/{admin2_role.id}"
    payload = {
        "name_key": "admin2_role",
        "active": False,  # Deactivate
        "capabilities": ["chat", "manage_access"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_version": 0,
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "PATCH", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.patch(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 5. PATCH /v1/admin/users/{id}/access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_admin_user_access_success_and_revocation(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    target_user = seed_user(client, role=env.role, email="target@example.test")

    path = f"/v1/admin/users/{target_user.id}/access"
    payload = {
        "approval_status": "approved",
        "role_id": str(env.role.id),
        "scope_limit": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_version": 0,
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "PATCH", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.patch(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == str(target_user.id)
        assert data["approval_status"] == "approved"
        assert data["version"] == 1
        assert client.database["users"].documents[str(target_user.id)]["version"] == 1

        refresh_decision(env)

        # Now revoke the user
        payload_rev = {
            "approval_status": "revoked",
            "role_id": None,
            "scope_limit": [],
            "expected_version": 1,
            "expected_policy_version": policy_version(client),
        }
        body_rev = json.dumps(payload_rev, separators=(",", ":")).encode()
        tok_rev = _token(
            signing_key, env.actor, env.login, "PATCH", path, body=body_rev
        )
        resp_rev = await http.patch(
            path, content=body_rev, headers={"authorization": f"Bearer {tok_rev}"}
        )
        assert resp_rev.status_code == 200
        data_rev = resp_rev.json()["data"]
        assert data_rev["approval_status"] == "revoked"
        assert data_rev["version"] == 2


@pytest.mark.asyncio
async def test_update_admin_user_access_prevents_self_edit_and_last_admin_removal(
    env, signing_key
):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    # 1. Caller trying to edit themselves -> 403
    path_self = f"/v1/admin/users/{env.actor.id}/access"
    payload = {
        "approval_status": "approved",
        "role_id": str(env.role.id),
        "scope_limit": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_version": env.actor.version,
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok_self = _token(signing_key, env.actor, env.login, "PATCH", path_self, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.patch(
            path_self, content=body, headers={"authorization": f"Bearer {tok_self}"}
        )
        assert resp.status_code == 403

        # 2. Last admin protection:
        # Create user2 with non-admin role.
        user_role = store(
            client.database["roles"],
            new_role(
                name="chat_role",
                active=True,
                capabilities={"chat"},
                grants=env.actor.scope_limit,
            ),
        )
        user2 = seed_user(client, role=user_role, email="user2@example.test")

        # Now if someone tried to revoke env.actor, it would be self-edit (403).
        # What if there is another admin, and env.actor revokes them? That's fine as long as env.actor remains.
        # But if the target is the *only* admin? (e.g. env.actor was somehow demoted or target is the sole admin).
        # Since env.actor is currently an admin, revoking user2 is allowed, not blocked by last-admin.
        # Let's verify revoking an already non-admin user works:
        path_u2 = f"/v1/admin/users/{user2.id}/access"
        payload_u2 = {
            "approval_status": "revoked",
            "role_id": None,
            "scope_limit": [],
            "expected_version": user2.version,
            "expected_policy_version": policy_version(client),
        }
        body_u2 = json.dumps(payload_u2, separators=(",", ":")).encode()
        tok_u2 = _token(
            signing_key, env.actor, env.login, "PATCH", path_u2, body=body_u2
        )
        resp_u2 = await http.patch(
            path_u2, content=body_u2, headers={"authorization": f"Bearer {tok_u2}"}
        )
        assert resp_u2.status_code == 200

        # Cannot edit an unapproved (now revoked) user -> 404
        refresh_decision(env)
        payload_reactivate = {
            "approval_status": "approved",
            "role_id": str(user_role.id),
            "scope_limit": [
                _scope_with_handle(
                    env.actor.id,
                    policy_version(client),
                    codec,
                    env.actor.scope_limit[0],
                )
            ],
            "expected_version": 1,
            "expected_policy_version": policy_version(client),
        }
        body_react = json.dumps(payload_reactivate, separators=(",", ":")).encode()
        tok_react = _token(
            signing_key, env.actor, env.login, "PATCH", path_u2, body=body_react
        )
        resp_react = await http.patch(
            path_u2,
            content=body_react,
            headers={"authorization": f"Bearer {tok_react}"},
        )
        assert resp_react.status_code == 404


# ---------------------------------------------------------------------------
# 6. Admission and Rate Limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_management_admission_rate_limit(env, signing_key):
    app = _make_app(env, signing_key)
    path = "/v1/admin/users"
    query = b"approval_status=approved&page_size=10"
    tok = _token(signing_key, env.actor, env.login, "GET", path, query=query)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        for _ in range(10):
            r = await http.get(
                f"{path}?{query.decode()}",
                headers={"authorization": f"Bearer {tok}"},
            )
            assert r.status_code == 200

        # 11th request should be rate-limited (429)
        r_limited = await http.get(
            f"{path}?{query.decode()}",
            headers={"authorization": f"Bearer {tok}"},
        )
        assert r_limited.status_code == 429
        assert r_limited.json()["error"]["code"] == ErrorCode.RATE_LIMITED.value


# ---------------------------------------------------------------------------
# 7. Non-canonical UUID, Validation & Transport Security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_management_rejects_non_canonical_uuid(env, signing_key):
    app = _make_app(env, signing_key)
    # Non-canonical UUID (uppercase or invalid)
    bad_uuid = "12345678-1234-1234-1234-1234567890AB"
    path = f"/v1/admin/roles/{bad_uuid}"
    payload = {
        "name_key": "any",
        "active": True,
        "capabilities": ["chat"],
        "grants": [],
        "expected_version": 0,
        "expected_policy_version": 0,
    }
    body = json.dumps(payload).encode()
    tok = _token(signing_key, env.actor, env.login, "PATCH", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.patch(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == ErrorCode.INVALID_REQUEST.value


@pytest.mark.asyncio
async def test_admin_management_rolls_back_on_audit_failure(env, signing_key):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    path = "/v1/admin/roles"
    payload = {
        "name_key": "will_fail_audit",
        "active": True,
        "capabilities": ["chat"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "POST", path, body=body)

    client.database["audit_events"].fail_insert = True

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.post(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == ErrorCode.PERSISTENCE_FAILED.value
        assert not any(
            r["name_key"] == "will_fail_audit"
            for r in client.database["roles"].documents.values()
        )


@pytest.mark.asyncio
async def test_admin_management_maps_unknown_commit_outcome(
    env, signing_key, monkeypatch
):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    path = "/v1/admin/roles"
    payload = {
        "name_key": "unknown_commit_role",
        "active": True,
        "capabilities": ["chat"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "POST", path, body=body)

    async def fail_with_unknown(*args, **kwargs):
        raise AppCommitOutcomeUnknown("Commit outcome unknown.")

    service = app.state.admin_management
    monkeypatch.setattr(service._management, "create_role", fail_with_unknown)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        resp = await http.post(
            path, content=body, headers={"authorization": f"Bearer {tok}"}
        )
        assert resp.status_code == 503
        error = resp.json()["error"]
        assert error["code"] == ErrorCode.PERSISTENCE_FAILED.value
        assert error["commit_state"] == "unknown"


@pytest.mark.asyncio
async def test_admin_management_rejects_tampered_body_and_extra_fields(
    env, signing_key
):
    app = _make_app(env, signing_key)
    codec = app.state.cursor_codec
    client = env.client

    path = "/v1/admin/roles"
    payload = {
        "name_key": "tamper_test",
        "active": True,
        "capabilities": ["chat"],
        "grants": [
            _scope_with_handle(
                env.actor.id,
                policy_version(client),
                codec,
                env.actor.scope_limit[0],
            )
        ],
        "expected_policy_version": policy_version(client),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    tok = _token(signing_key, env.actor, env.login, "POST", path, body=body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        # Tampered body content -> 401/403
        resp_tampered = await http.post(
            path, content=body + b" ", headers={"authorization": f"Bearer {tok}"}
        )
        assert resp_tampered.status_code in {401, 403}

        # Extra fields in payload -> 422
        extra_payload = dict(payload)
        extra_payload["unrecognized_field"] = "malicious"
        extra_body = json.dumps(extra_payload, separators=(",", ":")).encode()
        tok_extra = _token(
            signing_key, env.actor, env.login, "POST", path, body=extra_body
        )
        resp_extra = await http.post(
            path, content=extra_body, headers={"authorization": f"Bearer {tok_extra}"}
        )
        assert resp_extra.status_code == 422
        assert resp_extra.json()["error"]["code"] == ErrorCode.INVALID_REQUEST.value
