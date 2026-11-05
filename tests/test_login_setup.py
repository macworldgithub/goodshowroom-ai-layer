"""Local startup wiring checks with synthetic keys and database state."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import TypeAdapter
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.login_setup import build_login_services, previous_verification_keys
from goodshowroom_ai.storage.identities import Grant
from tests.test_identity_bridge import AUDIENCE, ISSUER, KID, _bootstrap_claims
from tests.test_identity_storage import role_record, selected_grant
from tests.workflow_fake import RollbackClient


@pytest.mark.asyncio
async def test_startup_builds_and_closes_configured_login_services(signing_key):
    client = RollbackClient([])
    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    client.database["roles"].documents[str(role.id)] = role.document()
    public_key = (
        signing_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    settings = Settings(
        identity_enabled=True,
        app_mongo_uri="mongodb://offline.invalid",
        identity_issuer=ISSUER,
        identity_audience=AUDIENCE,
        identity_key_id=KID,
        identity_public_key=public_key,
        identity_key_activated_at=int(datetime.now(UTC).timestamp()) - 1,
        identity_bootstrap_email="admin@example.test",
        identity_bootstrap_role_id=str(role.id),
        identity_bootstrap_scope=TypeAdapter(tuple[Grant, ...])
        .dump_json((selected_grant(),))
        .decode(),
    )
    app = create_app(settings, client_factory=lambda *_a, **_k: client)
    async with app.router.lifespan_context(app):
        assert app.state.login_workflow is not None
        assert app.state.login_lifecycle is not None
        assert app.state.temporary_memory is not None
        stamp = int(datetime.now(UTC).timestamp())
        path = "/v1/internal/login/exchange"
        claims = _bootstrap_claims(
            path=path,
            digest=operation_digest("POST", path),
            email="admin@example.test",
            iat=stamp,
            nbf=stamp,
            exp=stamp + 120,
            auth_started_at=stamp,
            auth_expires_at=stamp + 28800,
        )
        private_pem = signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        identity = {
            name: claims[name]
            for name in (
                "purpose",
                "provider",
                "provider_account_id",
                "email",
                "email_verified",
                "auth_started_at",
                "auth_expires_at",
                "login_session_id",
            )
        }
        script = """
import { signAssertion } from "./src/lib/server/identity.ts";
let input = ""; for await (const part of process.stdin) input += part;
const data = JSON.parse(input);
process.stdout.write(await signAssertion(data.config, data.identity, { method: "POST", path: "/v1/internal/login/exchange" }, data.now));
"""
        process = subprocess.run(
            [
                "node",
                "--conditions=react-server",
                "--experimental-strip-types",
                "--input-type=module",
                "-e",
                script,
            ],
            input=json.dumps(
                {
                    "config": {
                        "issuer": ISSUER,
                        "audience": AUDIENCE,
                        "kid": KID,
                        "privatePem": private_pem,
                        "activatedAt": stamp - 1,
                        "revoked": False,
                    },
                    "identity": identity,
                    "now": stamp,
                }
            ),
            text=True,
            capture_output=True,
            cwd=Path(__file__).parents[1] / "frontend",
            check=False,
        )
        if process.returncode:
            pytest.fail("Synthetic Node signer failed; output was kept private.")
        token = process.stdout
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test.invalid"
        ) as http:
            result = await http.post(path, headers={"authorization": f"Bearer {token}"})
            assert result.status_code == 200
            assert (
                result.json()["data"]["login_session_id"] == claims["login_session_id"]
            )
    assert app.state.app_database is None


@pytest.mark.asyncio
async def test_enabled_login_with_missing_configuration_fails_closed():
    client = RollbackClient([])
    app = create_app(
        Settings(identity_enabled=True, app_mongo_uri="mongodb://offline.invalid"),
        client_factory=lambda *_a, **_k: client,
    )
    with pytest.raises(ValueError, match="Login configuration unavailable"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.parametrize(
    "invalid",
    [
        {"identity_issuer": ""},
        {"identity_audience": ""},
        {"identity_key_id": "bad key"},
        {"identity_public_key": "invalid"},
        {"identity_key_activated_at": 0},
        {"identity_bootstrap_role_id": "invalid"},
        {"identity_bootstrap_scope": "invalid"},
    ],
)
def test_malformed_login_configuration_fails_closed(signing_key, invalid):
    public_key = (
        signing_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    values = {
        "identity_issuer": ISSUER,
        "identity_audience": AUDIENCE,
        "identity_key_id": KID,
        "identity_public_key": public_key,
        "identity_key_activated_at": int(datetime.now(UTC).timestamp()) - 1,
        "identity_bootstrap_email": "admin@example.test",
        "identity_bootstrap_role_id": str(role.id),
        "identity_bootstrap_scope": TypeAdapter(tuple[Grant, ...])
        .dump_json((selected_grant(),))
        .decode(),
    }
    values.update(invalid)
    with pytest.raises(ValueError, match="^Login configuration unavailable\\.$"):
        build_login_services(Settings(_env_file=None, **values), object())


def test_previous_key_configuration_enforces_rotation_and_compromise(signing_key):
    now = datetime.now(UTC)
    public = (
        signing_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    entry = {
        "kid": "old",
        "public_key": public,
        "activated_at": int(now.timestamp()) - 3600,
        "retired_at": int(now.timestamp()) - 10,
        "revoked": False,
    }
    keys = previous_verification_keys(
        json.dumps([entry]), current_key_id="new", now=now
    )
    assert keys["old"].public_key(
        now, now.replace(microsecond=0) - timedelta(seconds=11)
    )
    entry["revoked"] = True
    keys = previous_verification_keys(
        json.dumps([entry]), current_key_id="new", now=now
    )
    with pytest.raises(PermissionError):
        keys["old"].public_key(now, now)
    entry["revoked"] = False
    for invalid in (
        [entry, entry],
        [entry, entry | {"kid": "old-alias"}],
        [entry | {"kid": "new"}],
        [entry | {"unexpected": 1}],
        [entry | {"revoked": "false"}],
        [entry | {"retired_at": int(now.timestamp()) + 60}],
        [entry | {"activated_at": int(now.timestamp())}],
    ):
        with pytest.raises(ValueError, match="Login configuration unavailable"):
            previous_verification_keys(
                json.dumps(invalid), current_key_id="new", now=now
            )
    with pytest.raises(ValueError, match="Login configuration unavailable"):
        previous_verification_keys(
            '[{"kid":"old","kid":"alias"}]', current_key_id="new", now=now
        )


def test_startup_loads_previous_key_and_rejects_reused_key_material(signing_key):
    now = datetime.now(UTC)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)

    def public(key):
        return (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

    role = role_record(active=True, capabilities=frozenset({"manage_access"}))
    previous = [
        {
            "kid": "old",
            "public_key": public(signing_key),
            "activated_at": int(now.timestamp()) - 3600,
            "retired_at": int(now.timestamp()) - 10,
            "revoked": False,
        }
    ]
    values = {
        "identity_issuer": ISSUER,
        "identity_audience": AUDIENCE,
        "identity_key_id": KID,
        "identity_public_key": public(new_key),
        "identity_key_activated_at": int(now.timestamp()) - 1,
        "identity_bootstrap_email": "admin@example.test",
        "identity_bootstrap_role_id": str(role.id),
        "identity_bootstrap_scope": TypeAdapter(tuple[Grant, ...])
        .dump_json((selected_grant(),))
        .decode(),
        "identity_previous_keys": json.dumps(previous),
    }
    db = SimpleNamespace(
        load_authorization_policy=AsyncMock(), load_identity_security=AsyncMock()
    )
    workflow, _, _ = build_login_services(Settings(_env_file=None, **values), db)
    assert set(workflow._verifier.keys) == {KID, "old"}
    values["identity_public_key"] = public(signing_key)
    with pytest.raises(ValueError, match="Login configuration unavailable"):
        build_login_services(Settings(_env_file=None, **values), db)
