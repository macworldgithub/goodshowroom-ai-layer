"""Durable incident state. No private keys, tokens, or business grants."""

import hashlib
import re
from datetime import datetime

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ConfigDict, Field, model_validator


def public_key_fingerprint(key):
    return hashlib.sha256(
        key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).hexdigest()


class IdentitySecurityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    revoked_key_ids: tuple[str, ...] = Field(default=(), max_length=1000)
    revoked_public_keys: tuple[str, ...] = Field(default=(), max_length=1000)
    auth_cutoff: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_state(self):
        if (
            len(set(self.revoked_key_ids)) != len(self.revoked_key_ids)
            or len(set(self.revoked_public_keys)) != len(self.revoked_public_keys)
            or any(
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v)
                for v in self.revoked_key_ids
            )
            or any(
                not re.fullmatch(r"[a-f0-9]{64}", v) for v in self.revoked_public_keys
            )
            or bool(self.revoked_key_ids) != bool(self.revoked_public_keys)
            or bool(self.revoked_key_ids) != (self.auth_cutoff is not None)
        ):
            raise ValueError("Identity security state is unavailable")
        return self

    @classmethod
    def from_document(cls, document):
        # Old installations have no incident state. A present malformed value
        # must never become an empty registry. JSON revalidation handles BSON lists.
        import json

        if not isinstance(document, dict):
            raise ValueError("Identity security state is unavailable")
        raw = document.get("identity_security", {})
        return cls.model_validate_json(json.dumps(raw))

    def allows_login(self, auth_started_at: datetime):
        return (
            self.auth_cutoff is None
            or int(auth_started_at.timestamp()) > self.auth_cutoff
        )

    def allows_key(self, kid, fingerprint):
        return (
            kid not in self.revoked_key_ids
            and fingerprint not in self.revoked_public_keys
        )
