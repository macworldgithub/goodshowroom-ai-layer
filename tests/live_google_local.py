"""Run the explicitly authorized Google login against a disposable local DB.

This script never prints provider credentials, tokens, requests, or customer data.
It uses a synthetic role and no operational database. Stop it with Ctrl+C.
"""

import asyncio
import os
from pathlib import Path
import secrets
import sys
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values
from pydantic import TypeAdapter
from pymongo import AsyncMongoClient
import uvicorn

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.identities import Grant
from tests.test_identity_storage import role_record, selected_grant
from tests.live_local_ports import TestPortUnavailable, reserve_loopback

ROOT = Path(__file__).resolve().parents[1]
configuration = dotenv_values(ROOT / ".env.local")
client_id = configuration.get("GOOGLE_CLIENT_ID")
client_secret = configuration.get("GOOGLE_CLIENT_SECRET")
test_email = os.environ.get("LIVE_TEST_ADMIN_EMAIL", "")
scenario = os.environ.get("LIVE_TEST_LOGIN_SCENARIO", "bootstrap")
if (
    not client_id
    or not client_secret
    or not test_email
    or scenario not in {"bootstrap", "pending"}
):
    raise SystemExit("Local Google test configuration is incomplete.")

# This import installs the dedicated MongoDB-only backend network guard and
# removes ambient environment variables, including any production credentials.
from tests.isolated_login_mongo import DB_NAME, URI, verify_target  # noqa: E402

MARKER = {"_id": "ticket031-live-google", "purpose": "isolated-google-login"}
STOP_FILE = Path(
    r"C:\Users\Mustafa Abrar\.codex\visualizations\2026\11\02\01a19a9a-9782-7860-b680-7176db29eab8\ticket031-google-stop"
)


async def main():
    raw_client = AsyncMongoClient(URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    role = None
    owns = False
    role_created = False
    settings_created = False
    stop_requested = False
    node = None
    server = None
    server_task = None
    listener = None
    frontend_reservations = []
    try:
        if STOP_FILE.exists():
            raise RuntimeError("The test stop marker already exists.")
        await verify_target(raw_client)
        raw = raw_client[DB_NAME]
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
            raise RuntimeError("The isolated test database is not empty.")
        # A wildcard preflight also detects services bound to all IPv4
        # interfaces. The actual backend still binds only to loopback.
        with reserve_loopback("0.0.0.0", 8000):
            pass
        listener = reserve_loopback("127.0.0.1", 8000)
        listener.listen(128)
        # The OAuth callback uses localhost, which can resolve to IPv4 or IPv6.
        # Refuse to start if either address belongs to another local service.
        for address in ("0.0.0.0", "::1"):
            frontend_reservations.append(reserve_loopback(address, 3000))
        await raw.test_ownership.insert_one(MARKER)
        owns = True
        role = role_record(active=True, capabilities=frozenset({"manage_access"}))
        scope = TypeAdapter(tuple[Grant, ...]).dump_json((selected_grant(),)).decode()
        await raw.roles.insert_one(role.document())
        role_created = True
        await raw.app_settings.insert_one(
            {
                "_id": "access_control",
                "policy_version": 7,
                "commit_fence": 0,
                "bootstrap_complete": False,
            }
        )
        settings_created = True
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        activated = int(datetime.now(UTC).timestamp()) - 1
        settings = Settings(
            _env_file=None,
            app_env="test",
            identity_enabled=True,
            app_mongo_uri=URI,
            identity_issuer="isolated-next",
            identity_audience="isolated-backend",
            identity_key_id="isolated-live",
            identity_public_key=public,
            identity_key_activated_at=activated,
            identity_bootstrap_email=(
                test_email
                if scenario == "bootstrap"
                else "not-this-account@example.test"
            ),
            identity_bootstrap_role_id=str(role.id),
            identity_bootstrap_scope=scope,
        )
        app = create_app(settings)

        @app.middleware("http")
        async def report_login_status(request, call_next):
            response = await call_next(request)
            if request.url.path in {
                "/v1/internal/login/exchange",
                "/v1/internal/login/renew",
                "/v1/internal/login/revoke",
            }:
                print("Local login route status:", response.status_code, flush=True)
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
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                raise RuntimeError("The isolated backend did not start.")
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("The isolated backend did not start.")
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"}
        }
        environment.update(
            {
                "GOOGLE_CLIENT_ID": client_id,
                "GOOGLE_CLIENT_SECRET": client_secret,
                "APP_ENV": "test",
                "GOOGLE_TEST_FORCE_CONSENT": "true",
                "AUTH_SECRET": secrets.token_urlsafe(48),
                "NEXTAUTH_URL": "http://localhost:3000",
                "IDENTITY_ISSUER": "isolated-next",
                "IDENTITY_AUDIENCE": "isolated-backend",
                "IDENTITY_KEY_ID": "isolated-live",
                "IDENTITY_KEY_ACTIVATED_AT": str(activated),
                "IDENTITY_PRIVATE_KEY": private,
                "NEXT_TELEMETRY_DISABLED": "1",
                "NODE_OPTIONS": "--import="
                + (ROOT / "tests/live-google-network-guard.mjs").as_uri(),
            }
        )
        for reservation in frontend_reservations:
            reservation.close()
        frontend_reservations.clear()
        node = await asyncio.create_subprocess_exec(
            "node",
            "node_modules/next/dist/bin/next",
            "dev",
            "--webpack",
            "--hostname",
            "127.0.0.1",
            "--port",
            "3000",
            cwd=ROOT / "frontend",
            env=environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        print(
            "Started isolated Google login services. Awaiting frontend readiness.",
            flush=True,
        )
        while node.returncode is None:
            if STOP_FILE.exists():
                stop_requested = True
                break
            await asyncio.sleep(1)
        if node.returncode is not None:
            raise RuntimeError("The local frontend stopped.")
    finally:
        for reservation in frontend_reservations:
            reservation.close()
        if node is not None and node.returncode is None:
            node.terminate()
            await node.wait()
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            await server_task
        if listener is not None:
            listener.close()
        if owns:
            await verify_target(raw_client)
            raw = raw_client[DB_NAME]
            if await raw.test_ownership.find_one({"_id": MARKER["_id"]}) != MARKER:
                raise RuntimeError(
                    "Test ownership could not be verified; cleanup was skipped."
                )
            user = await raw.users.find_one({"email_key": test_email.lower()})
            if user is not None:
                await raw.login_sessions.delete_many({"user_id": user["_id"]})
                await raw.audit_events.delete_many({"actor_id": user["_id"]})
                await raw.users.delete_one(
                    {"_id": user["_id"], "email_key": test_email.lower()}
                )
            if role_created:
                await raw.roles.delete_one({"_id": str(role.id)})
            if settings_created:
                await raw.app_settings.delete_one({"_id": "access_control"})
            await raw.test_ownership.delete_one(MARKER)
        await raw_client.close()
        if stop_requested and STOP_FILE.is_file():
            STOP_FILE.unlink()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Isolated Google test stopped.")
    except TestPortUnavailable as error:
        print(str(error) + " Stop the service using it, then retry.", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print(
            "Isolated Google test failed; private details were withheld.",
            file=sys.stderr,
        )
        raise SystemExit(1)
