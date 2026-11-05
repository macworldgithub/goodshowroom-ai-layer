"""Signed, owner-only access status and request operations."""

import asyncio
from datetime import UTC
from uuid import UUID, uuid4

import jwt

from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.storage.access_requests import AccessRequestRecord, scope_within
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
    utc_datetime,
)
from goodshowroom_ai.storage.mongo import AppWriteConflict

_CAPABILITIES = frozenset(
    {
        StorageCapability.READ_IDENTITIES,
        StorageCapability.READ_LOGINS,
        StorageCapability.READ_POLICY,
        StorageCapability.READ_SETTINGS,
        StorageCapability.READ_ACCESS_REQUESTS,
        StorageCapability.WRITE_ACCESS_REQUESTS,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    }
)


class OwnAccessService:
    def __init__(self, db, verifier, admission, clock):
        self._db, self._verifier, self._admission, self._clock = (
            db,
            verifier,
            admission,
            clock,
        )

    @staticmethod
    def _context(token):
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError()
            subject = jwt.decode(token, options={"verify_signature": False})["sub"]
            actor = UUID(subject)
            if str(actor) != subject:
                raise ValueError()
            return InternalAuthorizationContext(
                actor_id=actor, capabilities=_CAPABILITIES
            )
        except Exception:
            raise IdentityDenied("Identity assertion denied.") from None

    async def _verify(self, tx, token, method, path):
        return await self._verifier.verify(
            token,
            purpose="request",
            method=method,
            path=path,
            now=utc_datetime(self._clock()),
            load_login=tx.get_login,
        )

    async def _snapshot(self, tx, token, method, path):
        claims = await self._verify(tx, token, method, path)
        user = await tx.get_user(UUID(claims["sub"]))
        if user is None or str(user.id) != claims["sub"]:
            raise AccessDenied("Access is not allowed.")
        version = await tx.get_policy_version()
        role = None
        sites = None
        if user.approval_status == "approved":
            role = await tx.get_role(user.role_id)
            if (
                role is None
                or not role.active
                or not user.scope_limit
                or any(
                    not any(scope_within(scope, allowed) for allowed in role.grants)
                    for scope in user.scope_limit
                )
            ):
                raise AccessDenied("Access is not allowed.")
            sites = await tx.get_setting("sites")
        await self._verify(tx, token, method, path)
        return claims, user, role, sites, version

    async def status(self, token):
        context = self._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            snapshot = await self._snapshot(tx, token, "GET", "/v1/me")
        async with self._db.snapshot_transaction(context) as tx:
            if await self._snapshot(tx, token, "GET", "/v1/me") != snapshot:
                raise AccessDenied("Access is not allowed.")
        _, user, role, sites, _ = snapshot
        allowed_sites = (
            {site.site_id for site in sites.sites} if sites is not None else set()
        )
        scopes = []
        if role is not None:
            for grant in user.scope_limit:
                site_ids = [site for site in grant.sites if site in allowed_sites]
                if site_ids:
                    scopes.append(
                        {
                            "dataset": grant.dataset,
                            "sites": site_ids,
                            "fields": list(grant.fields),
                            "tools": list(grant.tools),
                            "metrics": list(grant.metrics),
                            "record_mode": grant.record_mode,
                        }
                    )
        return {
            "access_status": user.approval_status,
            "role": role.name_key if role is not None else None,
            "capabilities": sorted(role.capabilities) if role is not None else [],
            "scope_choices": scopes,
        }

    async def request(self, token, request_id):
        if type(request_id) is not UUID:
            raise AccessDenied("Access is not allowed.")
        context = self._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            snapshot = await self._snapshot(tx, token, "POST", "/v1/access-requests")
        _, user, _, _, _ = snapshot
        await self._admission.admit_access_request(user.id)
        for attempt in range(3):
            try:
                snapshot, existing = await self._pending(context, token)
                if existing is not None:
                    confirmed, current = await self._pending(context, token)
                    if confirmed != snapshot or current != existing:
                        raise AppWriteConflict("Access state changed.")
                    return existing, False
                return await self._submit(context, token, request_id, snapshot)
            except AppWriteConflict:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))

    async def _pending(self, context, token):
        async with self._db.snapshot_transaction(context) as tx:
            snapshot = await self._snapshot(tx, token, "POST", "/v1/access-requests")
            _, user, _, _, _ = snapshot
            if user.approval_status != "pending":
                raise AccessDenied("Access is not allowed.")
            existing = await tx.find_active_access("request", user.email_key)
            if existing is not None and (
                existing.user_id != user.id
                or not existing.available(utc_datetime(self._clock()))
            ):
                raise AppWriteConflict("Access request changed.")
            await self._verify(tx, token, "POST", "/v1/access-requests")
            return snapshot, existing

    async def _submit(self, context, token, request_id, snapshot):
        claims, user, _, _, version = snapshot
        if user.approval_status != "pending":
            raise AccessDenied("Access is not allowed.")
        now = utc_datetime(self._clock()).astimezone(UTC)
        now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        candidate = AccessRequestRecord(
            id=uuid4(),
            kind="request",
            email_key=user.email_key,
            user_id=user.id,
            created_at=now,
            updated_at=now,
        )
        async with self._db.transaction(context) as tx:
            await tx.advance_access_control_fence(version)
            current = await self._verify(tx, token, "POST", "/v1/access-requests")
            if current != claims or await tx.get_user(user.id) != user:
                raise AppWriteConflict("Access state changed.")
            result = await tx.create_access_request(candidate, now)
            await self._verify(tx, token, "POST", "/v1/access-requests")
            await tx.append_required_audit(
                AppAuditEvent(
                    actor_id=user.id,
                    target_id=user.id,
                    action="access_control_change",
                    outcome="success",
                    request_id=request_id,
                    created_at=now,
                )
            )
            await self._verify(tx, token, "POST", "/v1/access-requests")
        async with self._db.snapshot_transaction(context) as tx:
            current = await self._verify(tx, token, "POST", "/v1/access-requests")
            if current != claims or await tx.get_user(user.id) != user:
                raise AccessDenied("Access is not allowed.")
            await self._verify(tx, token, "POST", "/v1/access-requests")
        return result, result.id == candidate.id
