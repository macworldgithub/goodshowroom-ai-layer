"""Loopback-only cross-service login check with synthetic state and keys."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import uvicorn
from pytest import MonkeyPatch
from starlette.responses import Response

from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import AssertionVerifier, VerificationKey
from goodshowroom_ai.login_lifecycle import LoginLifecycle
from goodshowroom_ai.storage.mongo import AppDatabase
from tests.test_access_workflow import Authorization
from tests.harness import block_network
from tests.test_identity_bridge import AUDIENCE, ISSUER, KID, _public_pem
from tests.test_identity_storage import role_record, selected_grant
from tests.workflow_fake import RollbackClient


async def main():
    # Bind first. A busy port fails without contacting an existing service.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 8000))
    listener.listen(128)
    network_guard = MonkeyPatch()
    block_network(network_guard)
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    await db.initialize_identity_indexes()
    await db.initialize_access_request_indexes()
    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    client.database["roles"].documents[str(role.id)] = role.document()
    now = datetime.now(UTC).replace(microsecond=0)
    stamp = int(now.timestamp())
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    verifier = AssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys={KID: VerificationKey(_public_pem(key), now - timedelta(seconds=1))},
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
        refresh_login=AsyncMock(),
        invalidate_login=AsyncMock(
            side_effect=[RuntimeError("synthetic failure"), None]
        ),
    )
    app = create_app(
        Settings(_env_file=None, app_env="test"),
        login_workflow=workflow,
        login_lifecycle=LoginLifecycle(db, verifier, memory, lambda: now),
    )
    observed = []
    renew_count = 0

    @app.middleware("http")
    async def observe(request, call_next):
        nonlocal renew_count
        if request.url.path == "/v1/internal/login/renew":
            renew_count += 1
            if renew_count == 3:
                observed.append((request.url.path, 503))
                return Response(status_code=503)
            if renew_count == 4:
                observed.append((request.url.path, "delayed"))
                await asyncio.sleep(6)
                return Response(status_code=503)
        response = await call_next(request)
        observed.append((request.url.path, response.status_code))
        return response

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=8000,
            access_log=False,
            log_config=None,
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                raise RuntimeError("Isolated backend failed to start")
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("Isolated backend did not start")
        login_id = str(uuid4())
        fixture = {
            "config": {
                "issuer": ISSUER,
                "audience": AUDIENCE,
                "kid": KID,
                "privatePem": key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
                "activatedAt": stamp - 1,
                "revoked": False,
            },
            "input": {
                "purpose": "bootstrap",
                "provider": "google",
                "provider_account_id": "synthetic-google-subject",
                "email": "admin@example.test",
                "email_verified": True,
                "auth_started_at": stamp,
                "auth_expires_at": stamp + 3600,
                "login_session_id": login_id,
            },
            "now": stamp,
        }
        keep = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"}
        environment = {k: v for k, v in os.environ.items() if k.upper() in keep}
        root = Path(__file__).parents[1]
        process = await asyncio.create_subprocess_exec(
            "node",
            "--disable-warning=ExperimentalWarning",
            "--conditions=react-server",
            "--experimental-strip-types",
            "--loader",
            (root / "tests/login-http-loader.mjs").as_uri(),
            str(root / "tests/login-http-client.mjs"),
            cwd=root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(
            process.communicate(json.dumps(fixture).encode()), timeout=30
        )
        if process.returncode != 0:
            raise RuntimeError(
                "Frontend HTTP check failed; private diagnostics were withheld"
            )
        assert observed.count(("/v1/internal/login/exchange", 200)) == 2
        assert ("/v1/internal/login/renew", 200) in observed
        assert ("/v1/internal/login/renew", 503) in observed
        assert ("/v1/internal/login/renew", "delayed") in observed
        assert ("/v1/internal/login/revoke", 500) in observed
        assert ("/v1/internal/login/revoke", 204) in observed
        assert (
            client.database["login_sessions"].documents[login_id]["revoked_at"]
            is not None
        )
        assert memory.invalidate_login.await_count == 2
        print(out.decode().strip())
        print(
            "PASS: observed backend routes and revoked state in the synthetic database"
        )
    finally:
        server.should_exit = True
        await task
        listener.close()
        await db.close()
        network_guard.undo()


if __name__ == "__main__":
    asyncio.run(main())
