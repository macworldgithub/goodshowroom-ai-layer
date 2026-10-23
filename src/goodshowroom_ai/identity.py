"""Server assertion boundary. No HTTP routes or ambient browser identity."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.models import utc_datetime


class IdentityDenied(PermissionError):
    """Safe denial without raw token, claims or key details."""


def operation_digest(
    method: str, path: str, query: bytes = b"", body: bytes = b""
) -> str:
    if (
        method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        or not re.fullmatch(r"/[A-Za-z0-9/_-]*", path)
        or "//" in path
    ):
        raise IdentityDenied("Identity assertion denied.")
    if (
        not isinstance(query, bytes)
        or not isinstance(body, bytes)
        or len(query) + len(body) > 65536
    ):
        raise IdentityDenied("Identity assertion denied.")
    return hashlib.sha256(len(query).to_bytes(4, "big") + query + body).hexdigest()


@dataclass(frozen=True, repr=False)
class VerificationKey:
    public_pem: bytes
    activated_at: datetime
    retired_at: datetime | None = None
    revoked: bool = False

    def public_key(self, now: datetime, issued: datetime):
        activated = utc_datetime(self.activated_at)
        if (
            self.revoked
            or issued < activated
            or issued >= activated + timedelta(days=90)
            or now < activated
        ):
            raise IdentityDenied("Identity assertion denied.")
        if self.retired_at is not None and (
            issued > utc_datetime(self.retired_at)
            or now >= self.retired_at + timedelta(seconds=210)
        ):
            raise IdentityDenied("Identity assertion denied.")
        key = load_pem_public_key(self.public_pem)
        if not isinstance(key, RSAPublicKey) or key.key_size != 3072:
            raise IdentityDenied("Identity assertion denied.")
        return key


class AssertionVerifier:
    def __init__(self, *, issuer: str, audience: str, keys: dict[str, VerificationKey]):
        if not issuer or not audience:
            raise ValueError("Identity configuration is required")
        self.issuer, self.audience, self.keys = issuer, audience, keys

    async def verify(
        self,
        token: str,
        *,
        purpose: str,
        method: str,
        path: str,
        now: datetime,
        load_login: Callable[[UUID], Awaitable[LoginRecord | None]],
        query: bytes = b"",
        body: bytes = b"",
    ) -> dict:
        try:
            now = utc_datetime(now)
            digest = operation_digest(method, path, query, body)
            if (
                not isinstance(token, str)
                or len(token) > 8192
                or purpose not in {"request", "bootstrap"}
            ):
                raise ValueError()
            header = jwt.get_unverified_header(token)
            if (
                set(header) != {"alg", "typ", "kid"}
                or header["alg"] != "RS256"
                or header["typ"] != "JWT"
                or not isinstance(header["kid"], str)
            ):
                raise ValueError()
            metadata = self.keys[header["kid"]]
            # This provisional key read authorizes nothing; signature and issuance are checked below.
            key = load_pem_public_key(metadata.public_pem)
            if not isinstance(key, RSAPublicKey) or key.key_size != 3072:
                raise ValueError()
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": ["iss", "aud", "iat", "nbf", "exp", "jti"],
                    "strict_aud": True,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            common = {
                "iss",
                "aud",
                "iat",
                "nbf",
                "exp",
                "jti",
                "purpose",
                "method",
                "path",
                "digest",
                "login_session_id",
            }
            fields = (
                {"sub", "generation"}
                if purpose == "request"
                else {
                    "provider",
                    "provider_account_id",
                    "email",
                    "email_verified",
                    "auth_started_at",
                    "auth_expires_at",
                }
            )
            if (
                set(claims) != common | fields
                or claims["purpose"] != purpose
                or claims["method"] != method
                or claims["path"] != path
                or claims["digest"] != digest
            ):
                raise ValueError()
            if any(type(claims[k]) is not int for k in ("iat", "nbf", "exp")):
                raise ValueError()
            timestamp = now.timestamp()
            if (
                not 0 < claims["exp"] - claims["iat"] <= 120
                or claims["nbf"] != claims["iat"]
                or claims["iat"] > timestamp + 30
                or timestamp >= claims["exp"] + 30
            ):
                raise ValueError()
            issued = datetime.fromtimestamp(claims["iat"], tz=now.tzinfo)
            metadata.public_key(now, issued)
            for field in ("jti", "login_session_id"):
                if str(UUID(claims[field])) != claims[field]:
                    raise ValueError()
            session = await load_login(UUID(claims["login_session_id"]))
            if session is not None and (
                not isinstance(session, LoginRecord)
                or str(session.id) != claims["login_session_id"]
            ):
                raise ValueError()
            if purpose == "request":
                if (
                    str(UUID(claims["sub"])) != claims["sub"]
                    or type(claims["generation"]) is not int
                    or session is None
                    or str(session.user_id) != claims["sub"]
                    or session.generation != claims["generation"]
                    or not session.valid(now)
                ):
                    raise ValueError()
            else:
                if (
                    claims["provider"] != "google"
                    or claims["email_verified"] is not True
                    or not isinstance(claims["provider_account_id"], str)
                    or not 1 <= len(claims["provider_account_id"]) <= 255
                    or not isinstance(claims["email"], str)
                    or not 3 <= len(claims["email"]) <= 254
                    or claims["email"].count("@") != 1
                    or not all(claims["email"].split("@"))
                    or any(c.isspace() for c in claims["email"])
                ):
                    raise ValueError()
                if (
                    any(
                        type(claims[k]) is not int
                        for k in ("auth_started_at", "auth_expires_at")
                    )
                    or not claims["auth_started_at"]
                    <= claims["iat"]
                    < claims["auth_expires_at"]
                    or timestamp < claims["auth_started_at"]
                    or timestamp
                    >= min(claims["auth_expires_at"], claims["auth_started_at"] + 28800)
                ):
                    raise ValueError()
                if session is not None and (
                    not session.valid(now)
                    or int(session.auth_started_at.timestamp())
                    != claims["auth_started_at"]
                    or int(session.auth_expires_at.timestamp())
                    != claims["auth_expires_at"]
                ):
                    raise ValueError()
            # Caller must recheck at protected release/commit; authorization is a separate service.
            if self.keys.get(header["kid"]) != metadata or metadata.revoked:
                raise ValueError()
            return claims
        except Exception:
            raise IdentityDenied("Identity assertion denied.") from None
