"""Fenced, audited role and employee-access management."""

from contextlib import asynccontextmanager
from uuid import UUID

from goodshowroom_ai.access_workflow import AccessWorkflow, _validated_scope
from goodshowroom_ai.authorization import AccessDenied, Principal, ProtectedAction
from goodshowroom_ai.storage.access_requests import AccessRequestRecord, scope_within
from goodshowroom_ai.storage.identities import RoleRecord, UserRecord
from goodshowroom_ai.storage.models import utc_datetime
from goodshowroom_ai.storage.mongo import AppWriteConflict


def _role_checked(role):
    try:
        if not isinstance(role, RoleRecord):
            raise ValueError()
        return RoleRecord.model_validate_json(role.model_dump_json(warnings="error"))
    except Exception:
        raise AccessDenied("Access is not allowed.") from None


def _within(grants, allowed):
    return all(
        any(scope_within(grant, parent) for parent in allowed) for grant in grants
    )


class AccessManagement:
    def __init__(self, db, authorization, clock):
        self._db, self._authorization, self._clock = db, authorization, clock

    @asynccontextmanager
    async def _change(self, principal, expected_policy_version, audit_request_id):
        if (
            not isinstance(principal, Principal)
            or type(expected_policy_version) is not int
            or expected_policy_version < 0
            or not isinstance(audit_request_id, UUID)
        ):
            raise AccessDenied("Access is not allowed.")
        decision = await self._authorization.authorize(
            principal, ProtectedAction(action="manage_access")
        )
        if decision.policy_version != expected_policy_version:
            raise AppWriteConflict("Access-control policy changed.")
        now = utc_datetime(self._clock())
        async with self._db.transaction(
            AccessWorkflow._context(principal.user_id)
        ) as tx:
            await tx.advance_access_control_fence(expected_policy_version)
            actor = await tx.get_user(principal.user_id)
            role = await tx.get_role(decision.snapshot.role.id)
            login = await tx.get_login(principal.login_id)
            if (
                actor != decision.snapshot.user
                or role != decision.snapshot.role
                or login != decision.snapshot.login
                or not login.valid(now)
            ):
                raise AccessDenied("Access is not allowed.")
            audit_target = []
            yield tx, decision, now, audit_target
            if len(audit_target) != 1 or not isinstance(audit_target[0], UUID):
                raise ValueError("Audit target is required")
            await self._authorization.recheck(decision)
            await tx.append_required_audit(
                AccessWorkflow._audit(
                    principal.user_id, audit_target[0], audit_request_id, now
                )
            )
            await self._authorization.recheck(decision)

    @staticmethod
    def _role_authority(role, decision):
        if not role.capabilities <= decision.snapshot.role.capabilities or not _within(
            role.grants, decision.snapshot.user.scope_limit
        ):
            raise AccessDenied("Access is not allowed.")

    async def _last_admin(
        self, tx, users, *, replacement_user=None, replacement_role=None
    ):
        for original in users:
            user = (
                replacement_user
                if replacement_user and original.id == replacement_user.id
                else original
            )
            if (
                user.approval_status != "approved"
                or user.role_id is None
                or not user.scope_limit
            ):
                continue
            role = (
                replacement_role
                if replacement_role and user.role_id == replacement_role.id
                else await tx.get_role(user.role_id)
            )
            if (
                role is not None
                and role.active
                and "manage_access" in role.capabilities
                and _within(user.scope_limit, role.grants)
            ):
                return
        raise AccessDenied("Access is not allowed.")

    async def create_role(
        self, principal, role, *, expected_policy_version, audit_request_id
    ):
        role = _role_checked(role)
        async with self._change(
            principal, expected_policy_version, audit_request_id
        ) as (tx, decision, now, audit_target):
            self._role_authority(role, decision)
            if role.version != 0:
                raise AccessDenied("Access is not allowed.")
            role = _role_checked(
                role.model_copy(update={"created_at": now, "updated_at": now})
            )
            await tx.create_role(role)
            audit_target.append(role.id)
        return role

    async def update_role(
        self,
        principal,
        role,
        *,
        expected_version,
        expected_policy_version,
        audit_request_id,
    ):
        role = _role_checked(role)
        async with self._change(
            principal, expected_policy_version, audit_request_id
        ) as (tx, decision, now, audit_target):
            old = await tx.get_role(role.id)
            if (
                old is None
                or type(expected_version) is not int
                or old.version != expected_version
            ):
                raise AppWriteConflict("Role version changed.")
            if (
                role.version != old.version + 1
                or role.created_at != old.created_at
                or role.id == decision.snapshot.role.id
            ):
                raise AccessDenied("Access is not allowed.")
            self._role_authority(old, decision)
            self._role_authority(role, decision)
            role = _role_checked(role.model_copy(update={"updated_at": now}))
            users = await tx.access_management_users()
            if role.active and any(
                user.role_id == role.id
                and user.approval_status == "approved"
                and not _within(user.scope_limit, role.grants)
                for user in users
            ):
                raise AccessDenied("Access is not allowed.")
            await self._last_admin(tx, users, replacement_role=role)
            await tx.update_role(role, expected_version)
            audit_target.append(role.id)
        return role

    async def change_user(
        self,
        principal,
        user_id,
        *,
        role_id,
        scope,
        status,
        expected_version,
        expected_policy_version,
        audit_request_id,
    ):
        if (
            not isinstance(principal, Principal)
            or not isinstance(user_id, UUID)
            or user_id == principal.user_id
            or type(status) is not str
            or status not in {"approved", "revoked"}
            or type(expected_version) is not int
        ):
            raise AccessDenied("Access is not allowed.")
        try:
            scope = _validated_scope(scope)
        except ValueError:
            raise AccessDenied("Access is not allowed.") from None
        async with self._change(
            principal, expected_policy_version, audit_request_id
        ) as (tx, decision, now, audit_target):
            current = await tx.get_user(user_id)
            if current is None or current.version != expected_version:
                raise AppWriteConflict("User version changed.")
            if current.approval_status != "approved":
                raise AccessDenied("Access is not allowed.")
            if current.role_id is not None:
                current_role = await tx.get_role(current.role_id)
                if (
                    current_role is None
                    or not current_role.capabilities
                    <= decision.snapshot.role.capabilities
                ):
                    raise AccessDenied("Access is not allowed.")
            if not _within(current.scope_limit, decision.snapshot.user.scope_limit):
                raise AccessDenied("Access is not allowed.")
            if status == "approved":
                if not isinstance(role_id, UUID) or not scope:
                    raise AccessDenied("Access is not allowed.")
                role = await tx.get_role(role_id)
                if (
                    role is None
                    or not role.active
                    or not role.capabilities <= decision.snapshot.role.capabilities
                    or not _within(scope, role.grants)
                    or not _within(scope, decision.snapshot.user.scope_limit)
                ):
                    raise AccessDenied("Access is not allowed.")
            elif role_id is not None or scope:
                raise AccessDenied("Access is not allowed.")
            updated = UserRecord.model_validate(
                {
                    **current.model_dump(),
                    "version": current.version + 1,
                    "updated_at": now,
                    "approval_status": status,
                    "role_id": role_id,
                    "scope_limit": scope,
                }
            )
            users = await tx.access_management_users()
            await self._last_admin(tx, users, replacement_user=updated)
            await tx.update_user_access(current, updated)
            audit_target.append(user_id)
        return updated

    async def create_preapproval(
        self, principal, record, *, expected_policy_version, audit_request_id
    ):
        try:
            if not isinstance(record, AccessRequestRecord):
                raise ValueError()
            record = AccessRequestRecord.model_validate_json(
                record.model_dump_json(warnings="error")
            )
        except Exception:
            raise AccessDenied("Access is not allowed.") from None
        async with self._change(
            principal, expected_policy_version, audit_request_id
        ) as (tx, decision, now, audit_target):
            if record.kind != "preapproval":
                raise AccessDenied("Access is not allowed.")
            record = AccessRequestRecord.model_validate(
                {
                    **record.model_dump(),
                    "created_at": now,
                    "updated_at": now,
                    "reviewed_by": principal.user_id,
                    "reviewed_at": now,
                }
            )
            role = await tx.get_role(record.role_id)
            if (
                role is None
                or not role.active
                or not role.capabilities <= decision.snapshot.role.capabilities
                or not _within(record.scope_limit, decision.snapshot.user.scope_limit)
            ):
                raise AccessDenied("Access is not allowed.")
            result = await tx.create_access_request(record, now)
            audit_target.append(result.id)
        return result

    async def revoke_preapproval(
        self, principal, request_id, *, expected_policy_version, audit_request_id
    ):
        if not isinstance(request_id, UUID):
            raise AccessDenied("Access is not allowed.")
        async with self._change(
            principal, expected_policy_version, audit_request_id
        ) as (tx, decision, now, audit_target):
            record = await tx.get_access_request(request_id)
            if (
                record is None
                or record.kind != "preapproval"
                or not record.available(now)
            ):
                raise AppWriteConflict("Preapproval changed.")
            role = await tx.get_role(record.role_id)
            if (
                role is None
                or not role.capabilities <= decision.snapshot.role.capabilities
                or not _within(record.scope_limit, decision.snapshot.user.scope_limit)
            ):
                raise AccessDenied("Access is not allowed.")
            await tx.close_access_request(request_id, status="revoked", now=now)
            audit_target.append(request_id)
