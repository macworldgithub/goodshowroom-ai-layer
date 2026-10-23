"""Synthetic boundary tests for the TICKET-001 identity assertion bridge."""

import base64
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from goodshowroom_ai.identity import (
    AssertionVerifier,
    IdentityDenied,
    VerificationKey,
    operation_digest,
)
from goodshowroom_ai.storage.logins import LoginRecord


NOW = datetime(2026, 10, 23, 9, 0, 0, tzinfo=UTC)
ISSUER = "goodshowroom-test-next"
AUDIENCE = "goodshowroom-test-api"
KID = "test-key-2026"
METHOD = "POST"
PATH = "/v1/internal/test"
QUERY = b"view=summary"
BODY = b'{"request":"synthetic"}'
USER_ID = uuid4()
LOGIN_ID = uuid4()


def _public_pem(private_key):
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _verifier(private_key, *, activated_at=None, retired_at=None, revoked=False):
    return AssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys={
            KID: VerificationKey(
                public_pem=_public_pem(private_key),
                activated_at=activated_at or NOW - timedelta(days=1),
                retired_at=retired_at,
                revoked=revoked,
            )
        },
    )


def _request_claims(**overrides):
    issued = int(NOW.timestamp())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued,
        "nbf": issued,
        "exp": issued + 120,
        "jti": str(uuid4()),
        "purpose": "request",
        "method": METHOD,
        "path": PATH,
        "digest": operation_digest(METHOD, PATH, QUERY, BODY),
        "login_session_id": str(LOGIN_ID),
        "sub": str(USER_ID),
        "generation": 2,
    }
    return claims | overrides


def _bootstrap_claims(**overrides):
    issued = int(NOW.timestamp())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued,
        "nbf": issued,
        "exp": issued + 120,
        "jti": str(uuid4()),
        "purpose": "bootstrap",
        "method": METHOD,
        "path": PATH,
        "digest": operation_digest(METHOD, PATH, QUERY, BODY),
        "login_session_id": str(LOGIN_ID),
        "provider": "google",
        "provider_account_id": "synthetic-google-subject",
        "email": "employee@example.invalid",
        "email_verified": True,
        "auth_started_at": issued - 600,
        "auth_expires_at": issued + 3600,
    }
    return claims | overrides


def _mint(private_key, claims=None, *, kid=KID, algorithm="RS256", headers=None):
    token_headers = {"kid": kid, "typ": "JWT"} | (headers or {})
    return jwt.encode(
        claims or _request_claims(),
        private_key,
        algorithm=algorithm,
        headers=token_headers,
    )


def _login(**overrides):
    return LoginRecord(
        id=LOGIN_ID,
        user_id=USER_ID,
        generation=2,
        auth_started_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW,
        auth_expires_at=NOW + timedelta(hours=2),
        expires_at=NOW + timedelta(hours=2),
        lease_valid_until=NOW + timedelta(minutes=5),
        **overrides,
    )


async def _load_login(_identifier):
    return _login()


async def _no_login(_identifier):
    return None


async def _verify(
    verifier,
    token,
    *,
    purpose="request",
    method=METHOD,
    path=PATH,
    query=QUERY,
    body=BODY,
    now=NOW,
    load_login=_load_login,
):
    return await verifier.verify(
        token,
        purpose=purpose,
        method=method,
        path=path,
        query=query,
        body=body,
        now=now,
        load_login=load_login,
    )


@pytest.mark.asyncio
async def test_request_assertion_accepts_generated_rs256_key_and_current_login(
    signing_key,
):
    verifier = _verifier(signing_key)
    claims = await _verify(verifier, _mint(signing_key))

    assert claims["sub"] == str(USER_ID)
    assert claims["generation"] == 2
    assert "role" not in claims
    assert "email" not in claims


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "algorithm"),
    [
        ({"alg": "RS512"}, "RS512"),
        ({"jku": "https://keys.invalid/jwks"}, "RS256"),
        ({"typ": "JWT+JSON"}, "RS256"),
    ],
)
async def test_rejects_non_rs256_or_extended_protected_headers(
    signing_key, header, algorithm
):
    verifier = _verifier(signing_key)
    token = _mint(signing_key, algorithm=algorithm, headers=header)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, token)


@pytest.mark.asyncio
async def test_rejects_unknown_key_id(signing_key):
    verifier = _verifier(signing_key)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key, kid="unknown-key"))


@pytest.mark.asyncio
async def test_rejects_forged_signature(signing_key):
    verifier = _verifier(signing_key)
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(attacker_key))


@pytest.mark.asyncio
async def test_rejects_changed_token_payload(signing_key):
    verifier = _verifier(signing_key)
    token = _mint(signing_key)
    head, _payload, signature = token.split(".")
    altered = base64.urlsafe_b64encode(b'{"purpose":"request"}').rstrip(b"=")
    forged = f"{head}.{altered.decode()}.{signature}"

    with pytest.raises(IdentityDenied):
        await _verify(verifier, forged)


@pytest.mark.asyncio
async def test_rejects_oversized_assertion_before_session_lookup(signing_key):
    verifier = _verifier(signing_key)
    calls = 0

    async def loader(_identifier):
        nonlocal calls
        calls += 1
        return _login()

    with pytest.raises(IdentityDenied):
        await _verify(verifier, "x" * 8193, load_login=loader)
    assert calls == 0


@pytest.mark.asyncio
async def test_rejects_wrong_issuer_or_audience(signing_key):
    verifier = _verifier(signing_key)

    for claims in (
        _request_claims(iss="untrusted-issuer"),
        _request_claims(aud="another-service"),
    ):
        with pytest.raises(IdentityDenied):
            await _verify(verifier, _mint(signing_key, claims))


@pytest.mark.asyncio
async def test_rejects_wrong_purpose(signing_key):
    verifier = _verifier(signing_key)

    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key, _request_claims(purpose="bootstrap")),
            purpose="request",
        )


@pytest.mark.asyncio
async def test_rejects_unsupported_purpose(signing_key):
    verifier = _verifier(signing_key)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key), purpose="admin")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"jti": "not-a-uuid"},
        {"login_session_id": "not-a-uuid"},
        {"sub": str(USER_ID).upper()},
    ],
)
async def test_rejects_invalid_or_noncanonical_uuid_claims(signing_key, overrides):
    verifier = _verifier(signing_key)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key, _request_claims(**overrides)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "now"),
    [
        ({"exp": int(NOW.timestamp())}, NOW),
        ({"exp": int(NOW.timestamp()) + 121}, NOW),
        ({"iat": int(NOW.timestamp()) + 31, "nbf": int(NOW.timestamp()) + 31}, NOW),
        ({"iat": int(NOW.timestamp()) + 30, "nbf": int(NOW.timestamp()) + 30}, NOW),
    ],
)
async def test_assertion_lifetime_and_clock_tolerance_boundaries(
    signing_key, claims, now
):
    verifier = _verifier(signing_key)
    assertion = _mint(signing_key, _request_claims(**claims))
    if claims.get("iat") == int(NOW.timestamp()) + 30:
        # The 30-second future boundary is permitted for assertion timestamps.
        accepted = await _verify(verifier, assertion, now=now)
        assert accepted["iat"] == int(now.timestamp()) + 30
    else:
        with pytest.raises(IdentityDenied):
            await _verify(verifier, assertion, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        {"method": "GET"},
        {"path": "/v1/internal/other"},
        {"query": b"view=detail"},
        {"body": b'{"request":"changed"}'},
    ],
)
async def test_rejects_method_path_query_or_body_mismatch(signing_key, operation):
    verifier = _verifier(signing_key)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key), **operation)


@pytest.mark.asyncio
async def test_rejects_noncanonical_or_oversized_operation_inputs(signing_key):
    verifier = _verifier(signing_key)
    token = _mint(signing_key)

    for values in (
        {"method": "OPTIONS"},
        {"path": "/v1//internal"},
        {"path": "/v1/internal?x=1"},
        {"query": "view=summary"},
        {"body": b"x" * 65537},
    ):
        with pytest.raises(IdentityDenied):
            await _verify(verifier, token, **values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 1},
        {"revoked_at": NOW},
        {"lease_valid_until": NOW},
        {"user_id": uuid4()},
    ],
)
async def test_rejects_wrong_generation_revoked_expired_or_wrong_owner_session(
    signing_key, overrides
):
    verifier = _verifier(signing_key)

    async def loader(_identifier):
        return _login(**overrides)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key), load_login=loader)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session",
    [
        object(),
        SimpleNamespace(id=uuid4(), user_id=USER_ID, generation=2),
    ],
)
async def test_rejects_malformed_or_wrong_session_record(signing_key, session):
    verifier = _verifier(signing_key)

    async def loader(_identifier):
        return session

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key), load_login=loader)


@pytest.mark.asyncio
async def test_assertion_clock_tolerance_does_not_extend_login_lease(signing_key):
    verifier = _verifier(signing_key)
    valid_until = NOW + timedelta(seconds=120)

    async def loader(_identifier):
        return _login(lease_valid_until=valid_until)

    # The assertion remains inside its 30-second expiry tolerance at +121s.
    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key),
            now=NOW + timedelta(seconds=121),
            load_login=loader,
        )


@pytest.mark.asyncio
async def test_key_registry_is_rechecked_after_async_session_read(signing_key):
    verifier = _verifier(signing_key)
    original = verifier.keys[KID]

    async def revoke_during_lookup(_identifier):
        verifier.keys[KID] = VerificationKey(
            public_pem=original.public_pem,
            activated_at=original.activated_at,
            revoked=True,
        )
        return _login()

    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key),
            load_login=revoke_during_lookup,
        )


@pytest.mark.asyncio
async def test_key_age_ends_at_90_days(signing_key):
    activated = NOW - timedelta(days=90)
    verifier = _verifier(signing_key, activated_at=activated)
    token = _mint(signing_key)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, token)


@pytest.mark.asyncio
async def test_key_is_valid_until_just_before_90_day_boundary(signing_key):
    activated = NOW - timedelta(days=90) + timedelta(seconds=1)
    verifier = _verifier(signing_key, activated_at=activated)

    claims = await _verify(verifier, _mint(signing_key))

    assert claims["purpose"] == "request"


@pytest.mark.asyncio
async def test_compromised_key_has_no_rotation_grace(signing_key):
    verifier = _verifier(signing_key, revoked=True)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key))


def test_retired_key_window_is_exactly_210_seconds(signing_key):
    retired_at = NOW
    metadata = VerificationKey(
        public_pem=_public_pem(signing_key),
        activated_at=NOW - timedelta(days=1),
        retired_at=retired_at,
    )

    assert metadata.public_key(NOW + timedelta(seconds=209), NOW) is not None
    with pytest.raises(IdentityDenied):
        metadata.public_key(NOW + timedelta(seconds=210), NOW)


@pytest.mark.asyncio
async def test_rejects_non_3072_bit_rsa_verification_key():
    small_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(small_key)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(small_key))


@pytest.mark.asyncio
async def test_bootstrap_accepts_only_verified_google_identity_fields(signing_key):
    verifier = _verifier(signing_key)
    token = _mint(signing_key, _bootstrap_claims())

    claims = await _verify(verifier, token, purpose="bootstrap", load_login=_no_login)

    assert claims["provider"] == "google"
    assert claims["email_verified"] is True
    assert "role" not in claims
    assert "generation" not in claims


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "other"},
        {"email_verified": False},
        {"email": "invalid"},
        {"provider_account_id": ""},
        {"role": "admin"},
    ],
)
async def test_rejects_unverified_or_extra_bootstrap_identity_claims(
    signing_key, overrides
):
    verifier = _verifier(signing_key)
    token = _mint(signing_key, _bootstrap_claims(**overrides))

    with pytest.raises(IdentityDenied):
        await _verify(verifier, token, purpose="bootstrap", load_login=_no_login)


@pytest.mark.asyncio
async def test_bootstrap_origin_expiry_and_eight_hour_limit_are_hard_bounds(
    signing_key,
):
    verifier = _verifier(signing_key)
    issued = int(NOW.timestamp())
    claims = _bootstrap_claims(
        auth_started_at=issued - 8 * 60 * 60,
        auth_expires_at=issued + 9 * 60 * 60,
    )

    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key, claims),
            purpose="bootstrap",
            load_login=_no_login,
        )


@pytest.mark.asyncio
async def test_bootstrap_does_not_apply_clock_skew_to_origin_auth_expiry(signing_key):
    verifier = _verifier(signing_key)
    issued = int(NOW.timestamp())
    claims = _bootstrap_claims(auth_expires_at=issued)

    with pytest.raises(IdentityDenied):
        await _verify(verifier, _mint(signing_key, claims), purpose="bootstrap")


@pytest.mark.asyncio
async def test_bootstrap_rejects_future_origin_login_start_within_assertion_skew(
    signing_key,
):
    verifier = _verifier(signing_key)
    issued = int(NOW.timestamp())
    future_start = issued + 10
    claims = _bootstrap_claims(
        iat=future_start,
        nbf=future_start,
        exp=future_start + 120,
        auth_started_at=future_start,
        auth_expires_at=issued + 3600,
    )

    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key, claims),
            purpose="bootstrap",
            load_login=_no_login,
        )


@pytest.mark.asyncio
async def test_bootstrap_rejects_revoked_persisted_login_binding(signing_key):
    verifier = _verifier(signing_key)

    async def loader(_identifier):
        return _login(
            auth_started_at=NOW - timedelta(minutes=10),
            auth_expires_at=NOW + timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
            revoked_at=NOW,
        )

    with pytest.raises(IdentityDenied):
        await _verify(
            verifier,
            _mint(signing_key, _bootstrap_claims()),
            purpose="bootstrap",
            load_login=loader,
        )


@pytest.mark.asyncio
async def test_node_signer_assertion_verifies_in_python_without_persisting_token(
    signing_key,
):
    frontend = Path(__file__).parents[1] / "frontend"
    issued = int(NOW.timestamp())
    identity = {
        "purpose": "bootstrap",
        "provider": "google",
        "provider_account_id": "synthetic-node-subject",
        "email": "employee@example.invalid",
        "email_verified": True,
        "auth_started_at": issued - 600,
        "auth_expires_at": issued + 3600,
        "login_session_id": str(LOGIN_ID),
    }
    operation = {
        "method": METHOD,
        "path": PATH,
        "query": list(QUERY),
        "body": list(BODY),
    }
    configuration = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "kid": KID,
        "privatePem": signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
        "activatedAt": issued - 3600,
        "revoked": False,
    }
    script = """
import { signAssertion } from "./src/lib/server/identity.ts";
let input = "";
for await (const part of process.stdin) input += part;
const value = JSON.parse(input);
const operation = {
  ...value.operation,
  query: new Uint8Array(value.operation.query),
  body: new Uint8Array(value.operation.body),
};
const token = await signAssertion(value.configuration, value.identity, operation, value.now);
process.stdout.write(token);
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
                "configuration": configuration,
                "identity": identity,
                "operation": operation,
                "now": issued,
            }
        ),
        text=True,
        capture_output=True,
        cwd=frontend,
        check=False,
    )
    if process.returncode != 0:
        pytest.fail("Node signer subprocess failed; process output was kept private.")

    # Keep the assertion in memory. Do not write or print the token.
    verifier = _verifier(signing_key, activated_at=NOW - timedelta(hours=1))
    claims = await _verify(
        verifier,
        process.stdout,
        purpose="bootstrap",
        load_login=_no_login,
    )

    assert claims["provider_account_id"] == "synthetic-node-subject"
    assert claims["digest"] == operation_digest(METHOD, PATH, QUERY, BODY)


def test_server_signer_rejects_default_non_server_import_condition():
    frontend = Path(__file__).parents[1] / "frontend"
    script = """
try {
  await import("./src/lib/server/identity.ts");
  process.exitCode = 0;
} catch {
  process.exitCode = 1;
}
"""
    process = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        cwd=frontend,
        check=False,
    )

    assert process.returncode == 1
