"""Loopback-only HTTP check with real TCP socket transport for signed POST and PATCH operations."""

import asyncio
import json
from pathlib import Path
import socket
import sys
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import uvicorn
from pytest import MonkeyPatch

from goodshowroom_ai.admin_access import (
    RecordHandlePosition,
    _RecordHandlePayload,
)
from goodshowroom_ai.admin_management import AdminManagement
from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.identity import (
    AssertionVerifier,
    VerificationKey,
    operation_digest,
)
from goodshowroom_ai.storage.mongo import AppDatabase
from tests.harness import block_network
from tests.test_access_management import (
    AdminPopulationCollection,
    new_role,
    policy_version,
    seed_user,
    store,
)
from tests.test_access_workflow import Authorization
from tests.test_identity_bridge import (
    AUDIENCE,
    ISSUER,
    KID,
    _mint,
    _public_pem,
    _request_claims,
)
from tests.test_identity_storage import (
    NOW,
    login_record,
)
from tests.workflow_fake import AccessRequestFakeCollection, RollbackClient


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


async def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(128)

    network_guard = MonkeyPatch()
    block_network(network_guard)

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

    admin_role = new_role(capabilities={"chat", "manage_access"})
    store(client.database["roles"], admin_role)
    actor = seed_user(
        client,
        role=admin_role,
        email="admin@example.test",
        provider="admin-sub",
    )
    login = login_record(actor.id)
    store(client.database["login_sessions"], login)

    target_user = seed_user(
        client,
        role=admin_role,
        email="target@example.test",
        provider="target-sub",
    )

    now = NOW
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    verifier = AssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys={KID: VerificationKey(_public_pem(key), now - timedelta(seconds=1))},
    )

    auth = Authorization()
    from types import SimpleNamespace

    auth.decision = SimpleNamespace(
        policy_version=policy_version(client),
        snapshot=SimpleNamespace(user=actor, role=admin_role, login=login),
    )

    service = AdminManagement(db, verifier, auth, lambda: now)
    codec = CursorCodec(Fernet.generate_key(), clock=lambda: now)

    app = create_app(
        Settings(_env_file=None, app_env="test"),
        admin_management=service,
    )
    app.state.cursor_codec = codec

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))

    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("Loopback test server failed to start")

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            # 1. POST /v1/admin/roles
            create_path = "/v1/admin/roles"
            create_payload = {
                "name_key": "analyst_role",
                "active": True,
                "capabilities": ["chat"],
                "grants": [
                    _scope_with_handle(
                        actor.id, policy_version(client), codec, actor.scope_limit[0]
                    )
                ],
                "expected_policy_version": policy_version(client),
            }
            create_body = json.dumps(create_payload, separators=(",", ":")).encode()
            tok_create = _token(
                key, actor, login, "POST", create_path, body=create_body
            )

            resp_create = await http.post(
                create_path,
                content=create_body,
                headers={"authorization": f"Bearer {tok_create}"},
            )
            assert resp_create.status_code == 201, resp_create.text
            role_data = resp_create.json()["data"]
            role_id = role_data["id"]
            assert role_data["version"] == 0
            assert role_data["policy_version"] == 8

            auth.decision.policy_version = policy_version(client)

            # 2. PATCH /v1/admin/roles/{role_id}
            patch_role_path = f"/v1/admin/roles/{role_id}"
            patch_role_payload = {
                "name_key": "senior_analyst_role",
                "active": True,
                "capabilities": ["chat"],
                "grants": [
                    _scope_with_handle(
                        actor.id, policy_version(client), codec, actor.scope_limit[0]
                    )
                ],
                "expected_version": 0,
                "expected_policy_version": policy_version(client),
            }
            patch_role_body = json.dumps(
                patch_role_payload, separators=(",", ":")
            ).encode()
            tok_patch_role = _token(
                key, actor, login, "PATCH", patch_role_path, body=patch_role_body
            )

            resp_patch_role = await http.patch(
                patch_role_path,
                content=patch_role_body,
                headers={"authorization": f"Bearer {tok_patch_role}"},
            )
            assert resp_patch_role.status_code == 200, resp_patch_role.text
            assert resp_patch_role.json()["data"]["version"] == 1

            auth.decision.policy_version = policy_version(client)

            # 3. PATCH /v1/admin/users/{user_id}/access
            patch_user_path = f"/v1/admin/users/{target_user.id}/access"
            patch_user_payload = {
                "approval_status": "approved",
                "role_id": role_id,
                "scope_limit": [
                    _scope_with_handle(
                        actor.id, policy_version(client), codec, actor.scope_limit[0]
                    )
                ],
                "expected_version": 0,
                "expected_policy_version": policy_version(client),
            }
            patch_user_body = json.dumps(
                patch_user_payload, separators=(",", ":")
            ).encode()
            tok_patch_user = _token(
                key, actor, login, "PATCH", patch_user_path, body=patch_user_body
            )

            resp_patch_user = await http.patch(
                patch_user_path,
                content=patch_user_body,
                headers={"authorization": f"Bearer {tok_patch_user}"},
            )
            assert resp_patch_user.status_code == 200, resp_patch_user.text
            user_data = resp_patch_user.json()["data"]
            assert user_data["version"] == 1
            assert user_data["approval_status"] == "approved"

            # 4. Tampered body transport rejection
            resp_tampered = await http.post(
                create_path,
                content=create_body + b" ",
                headers={"authorization": f"Bearer {tok_create}"},
            )
            assert resp_tampered.status_code in {401, 403}

            print(
                "PASS: isolated_role_management_http - loopback TCP socket POST and PATCH verified."
            )
    finally:
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
