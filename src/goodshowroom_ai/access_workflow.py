"""Verified identity resolution and fenced, audited access decisions.

The verifier and authorization service are trusted server dependencies.
Exchange can create a login binding in the same transaction as identity changes.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from goodshowroom_ai.authorization import AccessDenied, Principal, ProtectedAction
from goodshowroom_ai.storage.access_requests import scope_within
from goodshowroom_ai.storage.identities import Grant, UserRecord
from goodshowroom_ai.storage.logins import LoginRecord
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
        StorageCapability.WRITE_IDENTITIES,
        StorageCapability.READ_ACCESS_REQUESTS,
        StorageCapability.WRITE_ACCESS_REQUESTS,
        StorageCapability.READ_LOGINS,
        StorageCapability.WRITE_LOGINS,
        StorageCapability.READ_POLICY,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    }
)


def _validated_scope(scope):
    """Revalidate even objects created with Pydantic's unchecked helpers."""
    if (
        not isinstance(scope, tuple)
        or len(scope) > 100
        or any(not isinstance(item, Grant) for item in scope)
    ):
        raise ValueError("Invalid scope")
    try:
        return tuple(
            Grant.model_validate_json(item.model_dump_json(warnings="error"))
            for item in scope
        )
    except Exception:
        raise ValueError("Invalid scope") from None


class AccessWorkflow:
    def __init__(
        self,
        db,
        verifier,
        authorization,
        clock,
        bootstrap_email,
        bootstrap_role_id,
        bootstrap_scope,
    ):
        self._db, self._verifier, self._authorization, self._clock = (
            db,
            verifier,
            authorization,
            clock,
        )
        self._bootstrap_email = UserRecord.email(bootstrap_email)
        if not isinstance(bootstrap_role_id, UUID) or not isinstance(
            bootstrap_scope, tuple
        ):
            raise ValueError("Explicit bootstrap role and scope are required")
        bootstrap_scope = _validated_scope(bootstrap_scope)
        if not bootstrap_scope:
            raise ValueError("Explicit bootstrap role and scope are required")
        self._bootstrap_role_id, self._bootstrap_scope = (
            bootstrap_role_id,
            bootstrap_scope,
        )

    @staticmethod
    def _context(actor):
        return InternalAuthorizationContext(actor_id=actor, capabilities=_CAPABILITIES)

    def own_access_service(self, admission):
        from goodshowroom_ai.own_access import OwnAccessService

        return OwnAccessService(self._db, self._verifier, admission, self._clock)

    async def _verify(self, tx, token, now, *, load_login=None):
        return await self._verifier.verify(
            token,
            purpose="bootstrap",
            method="POST",
            path="/v1/internal/login/exchange",
            now=now,
            load_login=load_login or tx.get_login,
        )

    @staticmethod
    def _audit(actor, target, request_id, now):
        return AppAuditEvent(
            actor_id=actor,
            target_id=target,
            action="access_control_change",
            outcome="success",
            request_id=request_id,
            created_at=now,
        )

    async def resolve(self, token, *, expected_policy_version, request_id):
        return await self._resolve(
            token,
            expected_policy_version=expected_policy_version,
            request_id=request_id,
            create_login=False,
        )

    async def exchange(self, token, *, expected_policy_version, request_id):
        """Resolve identity and create its replay binding in one transaction."""
        return await self._resolve(
            token,
            expected_policy_version=expected_policy_version,
            request_id=request_id,
            create_login=True,
        )

    async def exchange_current(self, token, *, request_id):
        """Read the current policy version server-side before the fenced exchange."""
        async with self._db.snapshot_transaction(self._context(uuid4())) as tx:
            version = await tx.get_policy_version()
        return await self.exchange(
            token, expected_policy_version=version, request_id=request_id
        )

    async def _resolve(
        self, token, *, expected_policy_version, request_id, create_login
    ):
        if not isinstance(request_id, UUID) or type(expected_policy_version) is not int:
            raise AccessDenied("Access is not allowed.")
        now = utc_datetime(self._clock())
        # BSON dates retain milliseconds; values read back in this transaction
        # must compare equal to the identity created above them.
        now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        provisional = uuid4()
        async with self._db.snapshot_transaction(self._context(provisional)) as tx:

            async def no_login(_identifier):
                return None

            # Preliminary signature verification only; the scoped transaction
            # below verifies again against the actual persisted login binding.
            claims = await self._verify(tx, token, now, load_login=no_login)
            user = await tx.find_user_by_identity(
                claims["provider_account_id"], claims["email"]
            )
        actor = user.id if user is not None else provisional
        async with self._db.transaction(self._context(actor)) as tx:
            consumed = []
            await tx.advance_access_control_fence(expected_policy_version)
            current_claims = await self._verify(tx, token, utc_datetime(self._clock()))
            if current_claims != claims:
                raise AccessDenied("Access is not allowed.")
            current = await tx.find_user_by_identity(
                claims["provider_account_id"], claims["email"]
            )
            if current != user:
                raise AppWriteConflict("Identity changed.")
            login = await tx.get_login(UUID(claims["login_session_id"]))
            if login is not None and (user is None or login.user_id != user.id):
                raise AccessDenied("Access is not allowed.")
            if user is None:
                user = UserRecord(
                    id=actor,
                    provider_account_id=claims["provider_account_id"],
                    email_key=claims["email"],
                    created_at=now,
                    updated_at=now,
                )
                await tx.create_user(user)
            if user.approval_status == "pending" and login is None:
                bootstrap = (
                    user.email_key == self._bootstrap_email
                    and await tx.bootstrap_available()
                )
                pre = await tx.find_active_access("preapproval", user.email_key)
                pending = await tx.find_active_access("request", user.email_key)
                if pending is not None and pending.user_id != user.id:
                    raise AccessDenied("Access is not allowed.")
                if bootstrap:
                    role = await tx.get_role(self._bootstrap_role_id)
                    if role is None or "manage_access" not in role.capabilities:
                        raise AccessDenied("Access is not allowed.")
                    if pre is not None and (
                        not pre.available(now)
                        or pre.role_id != role.id
                        or pre.scope_limit != self._bootstrap_scope
                    ):
                        raise AccessDenied("Access is not allowed.")
                    user = await tx.apply_access_change(
                        user,
                        pending,
                        role=role,
                        scope=self._bootstrap_scope,
                        status="approved",
                        now=now,
                    )
                    if pre is not None:
                        await tx.close_access_for_decision(
                            pre,
                            status="approved",
                            now=now,
                            role=role,
                            scope=self._bootstrap_scope,
                        )
                        consumed.append(pre)
                    if pending is not None:
                        consumed.append(pending)
                    await tx.finish_bootstrap()
                elif pre is not None and pre.available(now):
                    role = await tx.get_role(pre.role_id)
                    user = await tx.apply_access_change(
                        user,
                        pre,
                        role=role,
                        scope=pre.scope_limit,
                        status="approved",
                        now=now,
                    )
                    consumed.append(pre)
                    if pending is not None:
                        await tx.close_access_for_decision(
                            pending,
                            status="approved",
                            now=now,
                            role=role,
                            scope=pre.scope_limit,
                        )
                        consumed.append(pending)
            if create_login:
                started = datetime.fromtimestamp(claims["auth_started_at"], tz=UTC)
                auth_expiry = datetime.fromtimestamp(claims["auth_expires_at"], tz=UTC)
                expiry = min(auth_expiry, started + timedelta(hours=8))
                candidate = LoginRecord(
                    id=UUID(claims["login_session_id"]),
                    user_id=user.id,
                    auth_started_at=started,
                    auth_expires_at=auth_expiry,
                    expires_at=expiry,
                    lease_valid_until=min(now + timedelta(minutes=5), expiry),
                    created_at=now,
                    updated_at=now,
                )
                login = await tx.exchange_login(candidate, now)
            await self._verify(tx, token, utc_datetime(self._clock()))
            await tx.append_required_audit(self._audit(actor, user.id, request_id, now))
            if any(
                not item.available(utc_datetime(self._clock())) for item in consumed
            ):
                raise AppWriteConflict("Access request expired.")
            await self._verify(tx, token, utc_datetime(self._clock()))
        return (user, login) if create_login else user

    async def decide(
        self,
        principal,
        request_id,
        *,
        approve,
        role_id,
        scope,
        expected_policy_version,
        audit_request_id,
    ):
        if (
            not isinstance(principal, Principal)
            or not isinstance(request_id, UUID)
            or not isinstance(audit_request_id, UUID)
            or type(approve) is not bool
            or type(expected_policy_version) is not int
            or not isinstance(scope, tuple)
            or any(not isinstance(g, Grant) for g in scope)
        ):
            raise AccessDenied("Access is not allowed.")
        try:
            scope = _validated_scope(scope)
        except ValueError:
            raise AccessDenied("Access is not allowed.") from None
        decision = await self._authorization.authorize(
            principal, ProtectedAction(action="manage_access")
        )
        if decision.policy_version != expected_policy_version:
            raise AppWriteConflict("Access-control policy changed.")
        now = utc_datetime(self._clock())
        async with self._db.transaction(self._context(principal.user_id)) as tx:
            await tx.advance_access_control_fence(expected_policy_version)
            actor = await tx.get_user(principal.user_id)
            actor_role = await tx.get_role(decision.snapshot.role.id)
            login = await tx.get_login(principal.login_id)
            if (
                actor != decision.snapshot.user
                or actor_role != decision.snapshot.role
                or login != decision.snapshot.login
                or not login.valid(now)
            ):
                raise AccessDenied("Access is not allowed.")
            request = await tx.get_access_request(request_id)
            if (
                request is None
                or request.kind != "request"
                or not request.available(now)
            ):
                raise AppWriteConflict("Access request changed.")
            if request.user_id == principal.user_id:
                raise AccessDenied("Access is not allowed.")
            user = await tx.get_user(request.user_id)
            if user is None or user.email_key != request.email_key:
                raise AccessDenied("Access is not allowed.")
            if approve:
                if not isinstance(role_id, UUID):
                    raise AccessDenied("Access is not allowed.")
                role = await tx.get_role(role_id)
                if (
                    role is None
                    or not role.active
                    or not role.capabilities <= decision.snapshot.role.capabilities
                    or not scope
                    or any(
                        not any(scope_within(g, a) for a in role.grants) for g in scope
                    )
                    or any(
                        not any(
                            scope_within(g, a)
                            for a in decision.snapshot.user.scope_limit
                        )
                        for g in scope
                    )
                ):
                    raise AccessDenied("Access is not allowed.")
            else:
                if role_id is not None or scope:
                    raise AccessDenied("Access is not allowed.")
                role = None
            result = await tx.apply_access_change(
                user,
                request,
                role=role,
                scope=scope,
                status="approved" if approve else "rejected",
                now=now,
            )
            await self._authorization.recheck(decision)
            await tx.append_required_audit(
                self._audit(principal.user_id, user.id, audit_request_id, now)
            )
            if not request.available(utc_datetime(self._clock())):
                raise AppWriteConflict("Access request expired.")
            await self._authorization.recheck(decision)
        return result
