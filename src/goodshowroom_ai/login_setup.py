"""Construct login services only from explicit server configuration."""

import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import TypeAdapter

from goodshowroom_ai.access_workflow import AccessWorkflow
from goodshowroom_ai.authorization import AuthorizationService
from goodshowroom_ai.identity import AssertionVerifier, VerificationKey
from goodshowroom_ai.login_lifecycle import LoginLifecycle
from goodshowroom_ai.storage.identities import Grant
from goodshowroom_ai.temporary import TemporaryMemoryStore


def previous_verification_keys(raw, *, current_key_id, now):
    """Read bounded server-only metadata; retired private keys are never loaded."""
    if not raw:
        return {}
    try:
        if not isinstance(raw, str) or len(raw) > 100_000:
            raise ValueError()

        def unique_fields(pairs):
            fields = {}
            for name, value in pairs:
                if name in fields:
                    raise ValueError()
                fields[name] = value
            return fields

        entries = json.loads(raw, object_pairs_hook=unique_fields)
        if not isinstance(entries, list) or len(entries) > 10:
            raise ValueError()
        result = {}
        seen_keys = set()
        for item in entries:
            if not isinstance(item, dict) or set(item) != {
                "kid",
                "public_key",
                "activated_at",
                "retired_at",
                "revoked",
            }:
                raise ValueError()
            kid = item["kid"]
            if (
                not isinstance(kid, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kid)
                or kid == current_key_id
                or kid in result
                or type(item["activated_at"]) is not int
                or type(item["retired_at"]) is not int
                or type(item["revoked"]) is not bool
                or not isinstance(item["public_key"], str)
                or len(item["public_key"]) > 8192
            ):
                raise ValueError()
            activated = datetime.fromtimestamp(item["activated_at"], UTC)
            retired = datetime.fromtimestamp(item["retired_at"], UTC)
            if not activated <= retired <= now or retired > activated + timedelta(
                days=90
            ):
                raise ValueError()
            public = item["public_key"].replace("\\n", "\n").encode()
            parsed = load_pem_public_key(public)
            if not isinstance(parsed, RSAPublicKey) or parsed.key_size != 3072:
                raise ValueError()
            numbers = parsed.public_numbers()
            if (numbers.n, numbers.e) in seen_keys:
                raise ValueError()
            seen_keys.add((numbers.n, numbers.e))
            result[kid] = VerificationKey(public, activated, retired, item["revoked"])
        return result
    except Exception:
        raise ValueError("Login configuration unavailable.") from None


def build_login_services(settings, db, memory=None):
    try:

        def clock():
            return datetime.now(UTC)

        if not settings.identity_issuer or not settings.identity_audience:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings.identity_key_id):
            raise ValueError()
        activated = datetime.fromtimestamp(settings.identity_key_activated_at, tz=UTC)
        key = VerificationKey(
            settings.identity_public_key.replace("\\n", "\n").encode(), activated
        )
        key.public_key(clock(), clock())
        keys = previous_verification_keys(
            settings.identity_previous_keys,
            current_key_id=settings.identity_key_id,
            now=clock(),
        )
        active_numbers = load_pem_public_key(key.public_pem).public_numbers()
        if any(
            load_pem_public_key(item.public_pem).public_numbers() == active_numbers
            for item in keys.values()
        ):
            raise ValueError()
        keys[settings.identity_key_id] = key
        verifier = AssertionVerifier(
            issuer=settings.identity_issuer,
            audience=settings.identity_audience,
            keys=keys,
            load_security=db.load_identity_security,
        )
        scope = TypeAdapter(tuple[Grant, ...]).validate_json(
            settings.identity_bootstrap_scope, strict=True
        )
        role_id = UUID(settings.identity_bootstrap_role_id)

        async def check_identity(principal):
            try:
                if verifier.locked:
                    return False
                policy = await db.load_authorization_policy(principal)
                return (
                    not verifier.locked
                    and policy.login.generation == principal.generation
                    and policy.login.valid(clock())
                )
            except Exception:
                return False

        authorization = AuthorizationService(
            load_policy=db.load_authorization_policy,
            check_identity=check_identity,
            clock=clock,
        )
        store = (
            memory
            if memory is not None
            else TemporaryMemoryStore(authorization=authorization, clock=clock)
        )
        workflow = AccessWorkflow(
            db,
            verifier,
            authorization,
            clock,
            settings.identity_bootstrap_email,
            role_id,
            scope,
        )
        return workflow, LoginLifecycle(db, verifier, store, clock), store
    except Exception:
        raise ValueError("Login configuration unavailable.") from None
