"""Fenced login renewal and revocation using signed server assertions."""

from datetime import UTC, datetime
from uuid import UUID

import jwt

from goodshowroom_ai.authorization import Principal
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
    utc_datetime,
)

_CAPABILITIES = frozenset(
    {
        StorageCapability.READ_LOGINS,
        StorageCapability.WRITE_LOGINS,
        StorageCapability.READ_POLICY,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    }
)


class LoginLifecycle:
    def __init__(self, db, verifier, memory, clock):
        if memory is None:
            raise ValueError("Login cleanup service is required")
        self._db, self._verifier, self._memory, self._clock = (
            db,
            verifier,
            memory,
            clock,
        )

    async def change(self, token, *, revoke, request_id):
        if type(revoke) is not bool or not isinstance(request_id, UUID):
            raise IdentityDenied("Identity assertion denied.")
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError()
            # Unverified data only selects a scoped lookup. It never grants access.
            candidate = jwt.decode(token, options={"verify_signature": False})
            subject = candidate["sub"]
            actor = UUID(subject)
            if str(actor) != subject:
                raise ValueError()
        except Exception:
            raise IdentityDenied("Identity assertion denied.") from None
        context = InternalAuthorizationContext(
            actor_id=actor, capabilities=_CAPABILITIES
        )
        path = "/v1/internal/login/revoke" if revoke else "/v1/internal/login/renew"
        now = utc_datetime(self._clock())
        async with self._db.snapshot_transaction(context) as tx:
            claims = await self._verifier.verify(
                token,
                purpose="request",
                method="POST",
                path=path,
                now=now,
                load_login=tx.get_login,
                allow_revoked_for_logout=revoke,
            )
            policy = await tx.get_policy_version()
        principal = Principal(
            user_id=actor,
            login_id=UUID(claims["login_session_id"]),
            generation=claims["generation"],
        )
        async with self._db.transaction(context) as tx:
            await tx.advance_access_control_fence(policy)
            current = await self._verifier.verify(
                token,
                purpose="request",
                method="POST",
                path=path,
                now=utc_datetime(self._clock()),
                load_login=tx.get_login,
                allow_revoked_for_logout=revoke,
            )
            if current != claims:
                raise IdentityDenied("Identity assertion denied.")
            login = await tx.get_login(principal.login_id)
            if revoke:
                result = await tx.revoke_login(
                    login.id,
                    generation=login.generation,
                    expected_version=login.version,
                    now=now,
                )
            else:
                result = await tx.renew_login(
                    login.id,
                    generation=login.generation,
                    expected_version=login.version,
                    now=now,
                    verified_auth_expiry=login.auth_expires_at,
                    assertion_issued_at=datetime.fromtimestamp(claims["iat"], tz=UTC),
                )
            await self._verifier.verify(
                token,
                purpose="request",
                method="POST",
                path=path,
                now=utc_datetime(self._clock()),
                load_login=tx.get_login,
                allow_revoked_for_logout=revoke,
            )
            await tx.append_required_audit(
                AppAuditEvent(
                    actor_id=actor,
                    target_id=actor,
                    action="security_outcome",
                    outcome="success",
                    request_id=request_id,
                    created_at=now,
                )
            )
        if revoke:
            await self._memory.invalidate_login(principal)
        else:
            await self._memory.refresh_login(principal)
        return result
