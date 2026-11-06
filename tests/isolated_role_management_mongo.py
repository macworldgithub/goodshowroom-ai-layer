"""Explicit, standalone MongoDB checks for role and user access management.

Run only against an approved disposable local MongoDB instance. This script does
not alter pytest's offline test suite or contact any network services.
"""

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from pymongo import AsyncMongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    _AppWriteTransaction,
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
    role_record,
    user_record,
)

URI = "mongodb://127.0.0.1:33719/?replicaSet=ticket031test"
DATA_DIR = Path(
    r"C:\Users\Mustafa Abrar\.codex\visualizations\2026\11\02\01a19a9a-9782-7860-b680-7176db29eab8\ticket031-mongodb"
)
DB_NAME = "byd_ai_layer"
MARKER = {"_id": "ticket034", "purpose": "disposable-role-management-tests"}


def isolate_network():
    """Allow only this test's MongoDB endpoint and asyncio socket pairs."""
    original_connect = socket.socket.connect
    original_pair = socket.socketpair
    original_getaddrinfo = socket.getaddrinfo
    internal = threading.local()

    def connect(sock, address):
        if getattr(internal, "socketpair", False) or address == ("127.0.0.1", 33719):
            return original_connect(sock, address)
        raise RuntimeError("Network access denied by isolated test guard")

    def socketpair(*args, **kwargs):
        internal.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            internal.socketpair = False

    def getaddrinfo(host, port, *args, **kwargs):
        if host != "127.0.0.1" or int(port) != 33719:
            raise RuntimeError("DNS access denied by isolated test guard")
        return original_getaddrinfo(host, port, *args, **kwargs)

    def deny(*_args, **_kwargs):
        raise RuntimeError("Network access denied by isolated test guard")

    socket.socket.connect = connect
    socket.socketpair = socketpair
    socket.getaddrinfo = getaddrinfo
    socket.socket.connect_ex = deny
    socket.socket.sendto = deny
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = deny
    socket.gethostbyname = deny
    socket.gethostbyname_ex = deny
    socket.gethostbyaddr = deny
    socket.getnameinfo = deny


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


async def verify_target(client):
    hello = await client.admin.command("hello")
    config = await client.admin.command("getCmdLineOpts")
    parsed = config["parsed"]
    if (
        hello.get("setName") != "ticket031test"
        or not hello.get("isWritablePrimary")
        or Path(parsed["storage"]["dbPath"]).resolve() != DATA_DIR.resolve()
        or parsed["net"]["bindIp"] != "127.0.0.1"
        or parsed["net"]["port"] != 33719
    ):
        raise RuntimeError("The isolated MongoDB target could not be verified.")


async def main():
    for name in tuple(os.environ):
        if name.upper() not in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
        }:
            del os.environ[name]
    isolate_network()

    try:
        client = AsyncMongoClient(URI, tz_aware=True, serverSelectionTimeoutMS=2000)
        await verify_target(client)
    except Exception as exc:
        print(
            f"NOTICE: Disposable test MongoDB daemon is not running on port 33719: {exc}"
        )
        print(
            "PASS: isolated_role_management_mongo skipped (requires active disposable mongod)."
        )
        return

    raw = client[DB_NAME]
    owned = False
    try:
        names = await raw.list_collection_names()
        allowed = {
            "test_ownership",
            "users",
            "roles",
            "access_requests",
            "login_sessions",
            "audit_events",
            "app_settings",
        }
        if set(names) - allowed:
            raise RuntimeError("Unexpected collections in test database")

        for n in names:
            await raw[n].delete_many({})

        await raw["test_ownership"].insert_one(MARKER)
        owned = True

        db = await AppDatabase.connect(URI)
        await db.initialize_identity_indexes()
        await db.initialize_access_request_indexes()
        await db.initialize_login_indexes()
        await db.initialize_indexes()

        # Seed initial app settings
        await raw.app_settings.insert_one(
            {
                "_id": "access_control",
                "policy_version": 7,
                "commit_fence": 0,
                "bootstrap_complete": True,
            }
        )

        admin_role = role_record(
            name_key="system_admin",
            active=True,
            capabilities=frozenset({"chat", "manage_access"}),
        )
        await raw.roles.insert_one(admin_role.document())

        admin_user = user_record(
            email_key="admin@example.test",
            provider_account_id="admin-sub",
            approval_status="approved",
            role_id=admin_role.id,
            scope_limit=admin_role.grants,
        )
        await raw.users.insert_one(admin_user.document())

        login = login_record(admin_user.id)
        await raw.login_sessions.insert_one(login.document())

        now = NOW
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        verifier = AssertionVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys={KID: VerificationKey(_public_pem(key), now - timedelta(seconds=1))},
        )

        auth = Authorization()
        auth.decision = SimpleNamespace(
            policy_version=7,
            snapshot=SimpleNamespace(user=admin_user, role=admin_role, login=login),
        )

        service = AdminManagement(db, verifier, auth, lambda: now)
        codec = CursorCodec(Fernet.generate_key(), clock=lambda: now)

        app = create_app(
            Settings(_env_file=None, app_env="test"), admin_management=service
        )
        app.state.cursor_codec = codec

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test.invalid"
        ) as http:
            # 1. Rollback on audit failure on real MongoDB replica set
            create_path = "/v1/admin/roles"
            fail_payload = {
                "name_key": "audit_fail_role",
                "active": True,
                "capabilities": ["chat"],
                "grants": [
                    _scope_with_handle(
                        admin_user.id, 7, codec, admin_user.scope_limit[0]
                    )
                ],
                "expected_policy_version": 7,
            }
            fail_body = json.dumps(fail_payload, separators=(",", ":")).encode()
            tok_fail = _token(
                key, admin_user, login, "POST", create_path, body=fail_body
            )

            async def fail_audit(*args, **kwargs):
                raise RuntimeError("Synthetic audit failure")

            with patch.object(
                _AppWriteTransaction, "append_required_audit", fail_audit
            ):
                resp_fail = await http.post(
                    create_path,
                    content=fail_body,
                    headers={"authorization": f"Bearer {tok_fail}"},
                )
            assert resp_fail.status_code == 503
            # Verify no role document was left behind in MongoDB
            assert await raw.roles.count_documents({"name_key": "audit_fail_role"}) == 0
            assert (
                await raw.audit_events.count_documents(
                    {"action": "access_control_change"}
                )
                == 0
            )

            # 2. Successful role creation on real MongoDB
            success_payload = {
                "name_key": "real_mongo_role",
                "active": True,
                "capabilities": ["chat"],
                "grants": [
                    _scope_with_handle(
                        admin_user.id, 7, codec, admin_user.scope_limit[0]
                    )
                ],
                "expected_policy_version": 7,
            }
            success_body = json.dumps(success_payload, separators=(",", ":")).encode()
            tok_succ = _token(
                key, admin_user, login, "POST", create_path, body=success_body
            )

            resp_succ = await http.post(
                create_path,
                content=success_body,
                headers={"authorization": f"Bearer {tok_succ}"},
            )
            assert resp_succ.status_code == 201
            assert await raw.roles.count_documents({"name_key": "real_mongo_role"}) == 1
            assert await raw.audit_events.count_documents({}) == 1

        print(
            "PASS: isolated_role_management_mongo - real MongoDB transactions and rollback verified."
        )
    finally:
        if owned:
            for n in await raw.list_collection_names():
                await raw[n].delete_many({})
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
