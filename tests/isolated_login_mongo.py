"""Explicit, standalone checks against the approved disposable local server.

Run only after confirming the server and its test-only data directory. This
script does not change pytest's offline guard or accept a caller-supplied URI.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pymongo import AsyncMongoClient

from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.admin_access import (
    AdminAccess,
    RecordHandlePosition,
    _RecordHandlePayload,
)
from goodshowroom_ai.authorization import AuthorizationService
from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import (
    AssertionVerifier,
    VerificationKey,
    operation_digest,
)
from goodshowroom_ai.login_lifecycle import LoginLifecycle
from goodshowroom_ai.admission import AdmissionController
from goodshowroom_ai.own_access import OwnAccessService
from goodshowroom_ai.key_incident import contain_signing_key_compromise
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.storage.mongo import AuthorizationDenied
from goodshowroom_ai.storage.models import (
    InternalAuthorizationContext,
    StorageCapability,
)
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppDatabaseUnavailable,
    AppWriteConflict,
    _AppWriteTransaction,
)
from tests.test_access_workflow import Authorization
from tests.test_identity_bridge import (
    AUDIENCE,
    ISSUER,
    KID,
    _bootstrap_claims,
    _mint,
    _public_pem,
    _request_claims,
)
from tests.test_identity_storage import role_record, selected_grant

URI = "mongodb://127.0.0.1:33719/?replicaSet=ticket031test"
DATA_DIR = Path(
    r"C:\Users\Mustafa Abrar\.codex\visualizations\2026\11\02\01a19a9a-9782-7860-b680-7176db29eab8\ticket031-mongodb"
)
DB_NAME = "byd_ai_layer"
MARKER = {"_id": "ticket031", "purpose": "disposable-synthetic-login-tests"}


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
    client = AsyncMongoClient(URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    db = None
    owned = False
    role = None
    login_ids = []
    settings_created = False
    try:
        await verify_target(client)
        raw = client[DB_NAME]
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
        if set(names) - allowed or any(
            [await raw[name].count_documents({}) for name in names]
        ):
            raise RuntimeError("The test database is not empty; no writes were made.")
        await raw["test_ownership"].insert_one(MARKER)
        owned = True
        db = await AppDatabase.connect(URI)
        await db.initialize_identity_indexes()
        await db.initialize_access_request_indexes()
        await db.initialize_login_indexes()
        await db.initialize_indexes()
        expected = {
            "users": {"uq_provider_account", "uq_email_key", "ix_approval_updated"},
            "roles": {"uq_name_key"},
            "access_requests": {"uq_active_kind_email", "ix_status_created"},
            "login_sessions": {"ix_user_revoked", "ttl_login_purge"},
            "audit_events": {"ix_actor_created", "ix_target_created"},
        }
        for collection, indexes in expected.items():
            actual = await raw[collection].index_information()
            assert indexes <= actual.keys()
        users_indexes = await raw.users.index_information()
        assert users_indexes["uq_email_key"]["key"] == [("email_key", 1)]
        assert users_indexes["uq_email_key"]["unique"] is True
        assert users_indexes["uq_provider_account"]["key"] == [
            ("provider", 1),
            ("provider_account_id", 1),
        ]
        assert users_indexes["uq_provider_account"]["unique"] is True
        assert users_indexes["ix_approval_updated"]["key"] == [
            ("approval_status", 1),
            ("updated_at", 1),
        ]
        roles_indexes = await raw.roles.index_information()
        assert roles_indexes["uq_name_key"]["key"] == [("name_key", 1)]
        assert roles_indexes["uq_name_key"]["unique"] is True
        access_indexes = await raw.access_requests.index_information()
        assert access_indexes["uq_active_kind_email"]["unique"] is True
        assert access_indexes["uq_active_kind_email"]["key"] == [
            ("kind", 1),
            ("email_key", 1),
        ]
        assert access_indexes["uq_active_kind_email"]["partialFilterExpression"] == {
            "active": True
        }
        login_indexes = await raw.login_sessions.index_information()
        assert login_indexes["ix_user_revoked"]["key"] == [
            ("user_id", 1),
            ("revoked_at", 1),
        ]
        assert login_indexes["ttl_login_purge"]["key"] == [("purge_after", 1)]
        assert login_indexes["ttl_login_purge"]["expireAfterSeconds"] == 0
        assert access_indexes["ix_status_created"]["key"] == [
            ("status", 1),
            ("created_at", 1),
        ]
        audit_indexes = await raw.audit_events.index_information()
        assert audit_indexes["ix_actor_created"]["key"] == [
            ("actor_id", 1),
            ("created_at", -1),
        ]
        assert audit_indexes["ix_target_created"]["key"] == [
            ("target_id", 1),
            ("created_at", -1),
        ]
        role = role_record(active=True, capabilities=frozenset({"manage_access"}))
        await raw.roles.insert_one(role.document())
        await raw.app_settings.insert_one(
            {
                "_id": "access_control",
                "policy_version": 7,
                "commit_fence": 0,
                "bootstrap_complete": False,
            }
        )
        settings_created = True
        now = datetime.now(UTC).replace(microsecond=123456)
        stamp = int(now.timestamp())
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        verifier = AssertionVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys={KID: VerificationKey(_public_pem(key), now - timedelta(minutes=1))},
            load_security=db.load_identity_security,
        )
        workflow = AccessWorkflow(
            db,
            verifier,
            Authorization(),
            lambda: now,
            "admin@example.test",
            role.id,
            (selected_grant(),),
        )
        memory = SimpleNamespace(
            refresh_login=AsyncMock(), invalidate_login=AsyncMock()
        )
        lifecycle = LoginLifecycle(db, verifier, memory, lambda: now)
        app = create_app(
            Settings(_env_file=None, app_env="test"),
            login_workflow=workflow,
            login_lifecycle=lifecycle,
        )
        exchange_path = "/v1/internal/login/exchange"
        login_id = str(uuid4())
        login_ids.append(login_id)
        claims = _bootstrap_claims(
            path=exchange_path,
            digest=operation_digest("POST", exchange_path),
            email="admin@example.test",
            login_session_id=login_id,
            iat=stamp,
            nbf=stamp,
            exp=stamp + 120,
            auth_started_at=stamp,
            auth_expires_at=stamp + 3600,
        )
        token = _mint(key, claims)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test.invalid"
        ) as http:

            async def post(path, assertion):
                return await http.post(
                    path, headers={"authorization": f"Bearer {assertion}"}
                )

            first = await post(exchange_path, token)
            assert first.status_code == 200, first.status_code
            data = first.json()["data"]
            repeated = await post(exchange_path, token)
            assert repeated.status_code == 200 and repeated.json()["data"] == data
            collision = _mint(
                key,
                claims
                | {
                    "provider_account_id": "other-synthetic-subject",
                    "login_session_id": str(uuid4()),
                },
            )
            assert (await post(exchange_path, collision)).status_code in (401, 403, 409)
            assert await raw.users.count_documents({}) == 1
            assert await raw.login_sessions.count_documents({}) == 1

            concurrent_id = str(uuid4())
            login_ids.append(concurrent_id)
            concurrent_token = _mint(key, claims | {"login_session_id": concurrent_id})
            results = await asyncio.gather(
                post(exchange_path, concurrent_token),
                post(exchange_path, concurrent_token),
            )
            assert any(result.status_code == 200 for result in results)
            assert all(result.status_code in (200, 409, 503) for result in results)
            retry = await post(exchange_path, concurrent_token)
            assert retry.status_code == 200
            assert retry.json()["data"]["login_session_id"] == concurrent_id
            assert await raw.login_sessions.count_documents({"_id": concurrent_id}) == 1
            try:
                await workflow.exchange(
                    concurrent_token, expected_policy_version=999999, request_id=uuid4()
                )
            except AppWriteConflict:
                pass
            else:
                raise AssertionError("A stale policy version did not conflict")
            assert (await post(exchange_path, concurrent_token)).status_code == 200

            def request_token(path):
                return _mint(
                    key,
                    _request_claims(
                        sub=data["user_id"],
                        login_session_id=login_id,
                        generation=data["generation"],
                        path=path,
                        digest=operation_digest("POST", path),
                        iat=stamp,
                        nbf=stamp,
                        exp=stamp + 120,
                    ),
                )

            renew_path = "/v1/internal/login/renew"
            revoke_path = "/v1/internal/login/revoke"
            assert (
                await post(renew_path, request_token(renew_path))
            ).status_code == 200
            before = await raw.login_sessions.find_one({"_id": login_id})

            async def fail_audit(*_args):
                raise RuntimeError("synthetic audit failure")

            with patch.object(
                _AppWriteTransaction, "append_required_audit", fail_audit
            ):
                failed = await post(revoke_path, request_token(revoke_path))
            assert failed.status_code == 503
            assert await raw.login_sessions.find_one({"_id": login_id}) == before
            assert (
                await post(revoke_path, request_token(revoke_path))
            ).status_code == 204
            assert (await raw.login_sessions.find_one({"_id": login_id}))[
                "revoked_at"
            ] is not None
            assert (await post(renew_path, request_token(renew_path))).status_code in (
                401,
                403,
            )
            # Exercise own-status and concurrent request recovery on real MongoDB.
            pending_login_id = str(uuid4())
            login_ids.append(pending_login_id)
            pending_token = _mint(
                key,
                claims
                | {
                    "email": "pending@example.test",
                    "provider_account_id": "synthetic-pending-subject",
                    "login_session_id": pending_login_id,
                },
            )
            pending_user, pending_login = await workflow.exchange_current(
                pending_token, request_id=uuid4()
            )
            assert pending_user.approval_status == "pending"
            own_service = OwnAccessService(
                db, verifier, AdmissionController(), lambda: now
            )
            own_app = create_app(
                Settings(_env_file=None, app_env="test"), own_access=own_service
            )

            def own_token(method, path):
                return _mint(
                    key,
                    _request_claims(
                        sub=str(pending_user.id),
                        login_session_id=str(pending_login.id),
                        generation=pending_login.generation,
                        method=method,
                        path=path,
                        digest=operation_digest(method, path),
                        iat=stamp,
                        nbf=stamp,
                        exp=stamp + 120,
                    ),
                )

            async with AsyncClient(
                transport=ASGITransport(app=own_app), base_url="http://test.invalid"
            ) as own_http:
                status = await own_http.get(
                    "/v1/me",
                    headers={"authorization": "Bearer " + own_token("GET", "/v1/me")},
                )
                assert (
                    status.status_code == 200
                    and status.json()["data"]["access_status"] == "pending"
                )
                assert status.json()["data"]["scope_choices"] == []
                request_headers = {
                    "authorization": "Bearer "
                    + own_token("POST", "/v1/access-requests")
                }
                with patch.object(
                    _AppWriteTransaction, "append_required_audit", fail_audit
                ):
                    assert (
                        await own_http.post(
                            "/v1/access-requests", headers=request_headers
                        )
                    ).status_code == 503
                assert (
                    await raw.access_requests.count_documents(
                        {"user_id": str(pending_user.id)}
                    )
                    == 0
                )
                responses = await asyncio.gather(
                    *(
                        own_http.post("/v1/access-requests", headers=request_headers)
                        for _ in range(6)
                    )
                )
                codes = sorted(response.status_code for response in responses)
                assert codes == [200, 200, 200, 200, 200, 201], codes
                assert all(
                    response.json()["data"] == responses[0].json()["data"]
                    for response in responses
                )
                assert (
                    await raw.access_requests.count_documents(
                        {"user_id": str(pending_user.id)}
                    )
                    == 1
                )
            print(
                "PASS: real MongoDB own-status, audit rollback, concurrent duplicate recovery"
            )

            # TICKET-033: use the still-active synthetic administrator login.
            async def valid_admin(principal):
                policy = await db.load_authorization_policy(principal)
                return (
                    policy.login.generation == principal.generation
                    and policy.login.valid(now)
                )

            admin_auth = AuthorizationService(
                load_policy=db.load_authorization_policy,
                check_identity=valid_admin,
                clock=lambda: now,
            )
            admin_workflow = AccessWorkflow(
                db,
                verifier,
                admin_auth,
                lambda: now,
                "admin@example.test",
                role.id,
                (selected_grant(),),
            )
            admin_service = AdminAccess(
                db, verifier, admin_auth, lambda: now, admin_workflow
            )
            admin_app = create_app(
                Settings(_env_file=None, app_env="test"), admin_access=admin_service
            )
            admin_app.state.cursor_codec = CursorCodec(
                Fernet.generate_key(), clock=lambda: now
            )
            policy_version = (
                await raw.app_settings.find_one({"_id": "access_control"})
            )["policy_version"]
            scope = selected_grant().model_dump(mode="json")
            position = RecordHandlePosition(
                actor=UUID(data["user_id"]),
                policy_version=policy_version,
                source=selected_grant().selected_records[0],
            )
            scope["selected_records"] = [
                admin_app.state.cursor_codec._encode_chat_position(
                    position, _RecordHandlePayload
                )
            ]
            body = json.dumps(
                {
                    "email": "ticket033-synthetic@example.test",
                    "role_id": str(role.id),
                    "scope_limit": [scope],
                    "expires_at": None,
                    "expected_policy_version": policy_version,
                },
                separators=(",", ":"),
            ).encode()
            admin_path = "/v1/admin/preapprovals"
            admin_token = _mint(
                key,
                _request_claims(
                    sub=data["user_id"],
                    login_session_id=concurrent_id,
                    generation=0,
                    method="POST",
                    path=admin_path,
                    digest=operation_digest("POST", admin_path, body=body),
                    iat=stamp,
                    nbf=stamp,
                    exp=stamp + 120,
                ),
            )
            async with AsyncClient(
                transport=ASGITransport(app=admin_app), base_url="http://test.invalid"
            ) as admin_http:
                headers = {"authorization": "Bearer " + admin_token}
                audit_filter = {
                    "actor_id": data["user_id"],
                    "action": "access_control_change",
                }
                audit_count = await raw.audit_events.count_documents(audit_filter)
                with patch.object(
                    _AppWriteTransaction, "append_required_audit", fail_audit
                ):
                    failed = await admin_http.post(
                        admin_path, content=body, headers=headers
                    )
                assert failed.status_code == 503, failed.status_code
                assert (
                    await raw.audit_events.count_documents(audit_filter) == audit_count
                )
                assert (
                    await raw.access_requests.count_documents(
                        {"email_key": "ticket033-synthetic@example.test"}
                    )
                    == 0
                )
                outcomes = await asyncio.gather(
                    *(
                        admin_http.post(admin_path, content=body, headers=headers)
                        for _ in range(2)
                    )
                )
                codes = sorted(item.status_code for item in outcomes)
                assert codes.count(201) == 1 and all(
                    code in (201, 409, 503) for code in codes
                ), codes
                assert (
                    await raw.audit_events.count_documents(audit_filter)
                    == audit_count + 1
                )
                assert (
                    await raw.access_requests.count_documents(
                        {"email_key": "ticket033-synthetic@example.test"}
                    )
                    == 1
                )
            print(
                "PASS: real MongoDB administrator preapproval audit rollback and concurrent duplicate safety"
            )
            # Incident recovery has no HTTP route. Use the trusted operator path.
            # An audit failure must roll back the cutoff and keep the process stopped.
            settings_before = await raw.app_settings.find_one({"_id": "access_control"})
            with patch.object(
                _AppWriteTransaction, "append_required_audit", fail_audit
            ):
                try:
                    await contain_signing_key_compromise(
                        db,
                        verifier,
                        SimpleNamespace(close=AsyncMock()),
                        kid=KID,
                        actor_id=UUID(data["user_id"]),
                        request_id=uuid4(),
                        expected_policy_version=settings_before["policy_version"],
                        now=now,
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Incident audit failure did not abort")
            assert (
                await raw.app_settings.find_one({"_id": "access_control"})
                == settings_before
            )
            assert verifier.locked
            state = await contain_signing_key_compromise(
                db,
                verifier,
                SimpleNamespace(close=AsyncMock()),
                kid=KID,
                actor_id=UUID(data["user_id"]),
                request_id=uuid4(),
                expected_policy_version=settings_before["policy_version"],
                now=now,
            )
            assert state.auth_cutoff == stamp
            assert (await post(exchange_path, token)).status_code in (401, 403)
            assert (await post(renew_path, request_token(renew_path))).status_code in (
                401,
                403,
            )
            # A new client and verifier simulate restart with stale key settings.
            restarted = await AppDatabase.connect(URI)
            try:
                stale = AssertionVerifier(
                    issuer=ISSUER,
                    audience=AUDIENCE,
                    keys=verifier.keys,
                    load_security=restarted.load_identity_security,
                )
                try:
                    await stale.verify(
                        token,
                        purpose="bootstrap",
                        method="POST",
                        path=exchange_path,
                        now=now,
                        load_login=AsyncMock(return_value=None),
                    )
                except IdentityDenied:
                    pass
                else:
                    raise AssertionError(
                        "A restarted verifier accepted the compromised key"
                    )
                context = InternalAuthorizationContext(
                    actor_id=UUID(data["user_id"]),
                    capabilities=frozenset({StorageCapability.READ_LOGINS}),
                )
                async with restarted.snapshot_transaction(context) as tx:
                    try:
                        await tx.get_login(UUID(concurrent_id))
                    except AuthorizationDenied:
                        pass
                    else:
                        raise AssertionError(
                            "A live pre-incident login survived restart"
                        )
            finally:
                await restarted.close()
            # A healthy replacement requires a new verified authentication time.
            replacement = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            fresh_verifier = AssertionVerifier(
                issuer=ISSUER,
                audience=AUDIENCE,
                keys={
                    "replacement": VerificationKey(
                        _public_pem(replacement), now - timedelta(minutes=1)
                    )
                },
                load_security=db.load_identity_security,
            )
            workflow = AccessWorkflow(
                db,
                fresh_verifier,
                Authorization(),
                lambda: now,
                "admin@example.test",
                role.id,
                (selected_grant(),),
            )
            old_auth = _mint(
                replacement,
                claims | {"login_session_id": str(uuid4())},
                kid="replacement",
            )
            current = await raw.app_settings.find_one({"_id": "access_control"})
            try:
                await workflow.exchange(
                    old_auth,
                    expected_policy_version=current["policy_version"],
                    request_id=uuid4(),
                )
            except (IdentityDenied, AuthorizationDenied):
                pass
            else:
                raise AssertionError("An old authentication recreated a login")
            now += timedelta(seconds=2)
            new_stamp = int(now.timestamp())
            fresh_id = str(uuid4())
            login_ids.append(fresh_id)
            fresh_token = _mint(
                replacement,
                claims
                | {
                    "login_session_id": fresh_id,
                    "iat": new_stamp,
                    "nbf": new_stamp,
                    "exp": new_stamp + 120,
                    "auth_started_at": new_stamp,
                },
                kid="replacement",
            )
            await workflow.exchange(
                fresh_token,
                expected_policy_version=current["policy_version"],
                request_id=uuid4(),
            )
            # Competing incident transactions share the same policy fence.
            current = await raw.app_settings.find_one({"_id": "access_control"})

            async def incident():
                return await contain_signing_key_compromise(
                    db,
                    fresh_verifier,
                    SimpleNamespace(close=AsyncMock()),
                    kid="replacement",
                    actor_id=UUID(data["user_id"]),
                    request_id=uuid4(),
                    expected_policy_version=current["policy_version"],
                    now=now,
                )

            context = InternalAuthorizationContext(
                actor_id=UUID(data["user_id"]),
                capabilities=frozenset(
                    {
                        StorageCapability.READ_LOGINS,
                        StorageCapability.READ_POLICY,
                        StorageCapability.ADVANCE_ACCESS_CONTROL,
                        StorageCapability.WRITE_REQUIRED_AUDIT,
                    }
                ),
            )
            try:
                async with db.snapshot_transaction(context) as tx:
                    assert (await tx.get_login(UUID(fresh_id))).valid(now)
                    results = await asyncio.gather(
                        incident(), incident(), return_exceptions=True
                    )
                    # This old work snapshot cannot commit after incident recovery.
                    await tx.advance_access_control_fence(current["policy_version"])
            except (AppWriteConflict, AppDatabaseUnavailable):
                pass
            else:
                raise AssertionError("An in-flight snapshot crossed the incident fence")
            assert sum(not isinstance(item, BaseException) for item in results) == 1
            assert (
                sum(
                    isinstance(item, (AppWriteConflict, RuntimeError))
                    for item in results
                )
                == 1
            )
            durable = await db.load_identity_security()
            assert set(durable.revoked_key_ids) == {KID, "replacement"}
            assert durable.auth_cutoff == new_stamp
            print(
                "PASS: real MongoDB incident audit rollback, durable key/login denial after reconnect, fresh authentication, incident concurrency and stale-work commit denial"
            )
        print(
            "PASS: real MongoDB indexes, exchange replay, collision, concurrency, renewal, audit rollback, revocation and retry"
        )
    finally:
        if db is not None:
            await db.close()
        if owned:
            await verify_target(client)
            raw = client[DB_NAME]
            if await raw.test_ownership.find_one({"_id": "ticket031"}) != MARKER:
                raise RuntimeError(
                    "Test ownership marker changed; cleanup was not attempted."
                )
            synthetic_user = await raw.users.find_one(
                {
                    "provider_account_id": "synthetic-google-subject",
                    "email_key": "admin@example.test",
                }
            )
            if synthetic_user is not None:
                await raw.audit_events.delete_many({"actor_id": synthetic_user["_id"]})
                await raw.users.delete_one(
                    {
                        "_id": synthetic_user["_id"],
                        "provider_account_id": "synthetic-google-subject",
                    }
                )
            await raw.access_requests.delete_many(
                {"kind": "preapproval", "email_key": "ticket033-synthetic@example.test"}
            )
            pending_user = await raw.users.find_one(
                {
                    "provider_account_id": "synthetic-pending-subject",
                    "email_key": "pending@example.test",
                }
            )
            if pending_user is not None:
                await raw.access_requests.delete_many(
                    {
                        "user_id": pending_user["_id"],
                        "email_key": "pending@example.test",
                    }
                )
                await raw.audit_events.delete_many({"actor_id": pending_user["_id"]})
                await raw.users.delete_one(
                    {
                        "_id": pending_user["_id"],
                        "provider_account_id": "synthetic-pending-subject",
                    }
                )
            if login_ids:
                await raw.login_sessions.delete_many({"_id": {"$in": login_ids}})
            if role is not None:
                await raw.roles.delete_one({"_id": str(role.id)})
            if settings_created:
                await raw.app_settings.delete_one({"_id": "access_control"})
            for collection in (
                "users",
                "roles",
                "access_requests",
                "login_sessions",
                "audit_events",
                "app_settings",
            ):
                if await raw[collection].count_documents({}):
                    raise RuntimeError(
                        "Unremoved records remain; the ownership marker was retained."
                    )
            await raw.test_ownership.delete_one(MARKER)
            print("PASS: synthetic test records removed; indexes retained")
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
