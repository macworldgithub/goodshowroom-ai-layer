"""Identity boundary and orchestration for administrator role and user access management."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from goodshowroom_ai.access_management import AccessManagement, within_scope
from goodshowroom_ai.admin_access import (
    RecordHandlePosition,
    _RecordHandlePayload,
)
from goodshowroom_ai.api_contracts import (
    AdminRoleItem,
    AdminScopeGrant,
    AdminUserItem,
    ErrorCode,
)
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.authorization import (
    AccessDenied,
    Contract,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.cursors import CursorRejected, CursorUnavailable
from goodshowroom_ai.own_access import OwnAccessService
from goodshowroom_ai.storage.identities import Grant, RoleRecord
from goodshowroom_ai.storage.models import utc_datetime
from goodshowroom_ai.storage.mongo import AppWriteConflict


class AdminUsersPagePosition(Contract):
    actor: UUID
    policy_version: int = Field(ge=0)
    status: Literal["pending", "approved", "rejected", "revoked"]
    page_size: int = Field(ge=1, le=100)
    after: UUID


class _AdminUsersPagePayload(Contract):
    purpose: Literal["goodshowroom.admin-users-page.v1"] = (
        "goodshowroom.admin-users-page.v1"
    )
    position: AdminUsersPagePosition


class AdminRolesPagePosition(Contract):
    actor: UUID
    policy_version: int = Field(ge=0)
    active: bool | None = None
    page_size: int = Field(ge=1, le=100)
    after: UUID


class _AdminRolesPagePayload(Contract):
    purpose: Literal["goodshowroom.admin-roles-page.v1"] = (
        "goodshowroom.admin-roles-page.v1"
    )
    position: AdminRolesPagePosition


def _grant_to_wire(
    grant: Grant, principal: Principal, policy_version: int, codec
) -> AdminScopeGrant:
    selected_handles = []
    if grant.record_mode == "selected":
        if codec is None:
            raise CursorUnavailable()
        for source in grant.selected_records:
            pos = RecordHandlePosition(
                actor=principal.user_id,
                policy_version=policy_version,
                source=source,
            )
            selected_handles.append(
                codec._encode_chat_position(pos, _RecordHandlePayload)
            )
    return AdminScopeGrant(
        dataset=grant.dataset,
        sites=grant.sites,
        record_mode=grant.record_mode,
        selected_records=tuple(selected_handles),
        fields=grant.fields,
        metrics=grant.metrics,
        tools=grant.tools,
    )


def _resolve_scope(scope, principal, version, codec):
    result = []
    for item in scope:
        values = item.model_dump()
        selected = []
        for handle in item.selected_records:
            if codec is None:
                raise CursorUnavailable()
            position = codec._decode_chat_position(handle, _RecordHandlePayload)
            if (
                position.actor != principal.user_id
                or position.policy_version != version
            ):
                raise CursorRejected(policy_changed=position.policy_version != version)
            selected.append(position.source)
        values["selected_records"] = tuple(selected)
        try:
            result.append(Grant.model_validate(values))
        except ValueError:
            raise CursorRejected() from None
    return tuple(result)


class AdminManagement:
    """Coordinate verified identity, safe management lists/projection, handle conversion, existing mutation services, and result readback."""

    def __init__(self, db, verifier, authorization, clock):
        self._db = db
        self._verifier = verifier
        self._authorization = authorization
        self._clock = clock
        self._management = AccessManagement(db, authorization, clock)

    async def authorize(self, token, *, method, path, query=b"", body=b""):
        context = OwnAccessService._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            claims = await self._verifier.verify(
                token,
                purpose="request",
                method=method,
                path=path,
                query=query,
                body=body,
                now=utc_datetime(self._clock()),
                load_login=tx.get_login,
            )
        principal = Principal(
            user_id=UUID(claims["sub"]),
            login_id=UUID(claims["login_session_id"]),
            generation=claims["generation"],
        )
        decision = await self._authorization.authorize(
            principal, ProtectedAction(action="manage_access")
        )
        return principal, decision

    async def list_users(self, token, *, path, query, status, page_size, cursor, codec):
        principal, decision = await self.authorize(
            token, method="GET", path=path, query=query
        )
        if codec is None:
            raise CursorUnavailable()
        after = None
        if cursor:
            position = codec._decode_chat_position(cursor, _AdminUsersPagePayload)
            if (
                position.actor != principal.user_id
                or position.status != status
                or position.page_size != page_size
                or position.policy_version != decision.policy_version
            ):
                raise CursorRejected(
                    policy_changed=position.policy_version != decision.policy_version
                )
            after = position.after

        context = OwnAccessService._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            if await tx.get_policy_version() != decision.policy_version:
                raise CursorRejected(policy_changed=True)
            all_users = await tx.access_management_users()

            roles_cache = {}

            async def get_cached_role(r_id):
                if r_id not in roles_cache:
                    roles_cache[r_id] = await tx.get_role(r_id)
                return roles_cache[r_id]

            manageable_users = []
            for u in all_users:
                if u.approval_status != status:
                    continue
                if u.approval_status == "approved":
                    if u.role_id is None:
                        continue
                    r = await get_cached_role(u.role_id)
                    if (
                        r is None
                        or not (r.capabilities <= decision.snapshot.role.capabilities)
                        or not within_scope(
                            u.scope_limit, decision.snapshot.user.scope_limit
                        )
                    ):
                        continue
                manageable_users.append(u)

        await self._authorization.recheck(decision)

        manageable_users.sort(key=lambda u: str(u.id))

        start_index = 0
        if after is not None:
            after_str = str(after)
            for idx, u in enumerate(manageable_users):
                if str(u.id) > after_str:
                    start_index = idx
                    break
            else:
                start_index = len(manageable_users)

        page_users = manageable_users[start_index : start_index + page_size]

        items = []
        for u in page_users:
            if u.approval_status == "approved":
                tokenized_scope = [
                    _grant_to_wire(g, principal, decision.policy_version, codec)
                    for g in u.scope_limit
                ]
                items.append(
                    AdminUserItem(
                        id=u.id,
                        email=u.email_key,
                        display_name=u.display_name,
                        approval_status=u.approval_status,
                        version=u.version,
                        role_id=u.role_id,
                        scope_limit=tuple(tokenized_scope),
                    )
                )
            else:
                items.append(
                    AdminUserItem(
                        id=u.id,
                        email=u.email_key,
                        display_name=u.display_name,
                        approval_status=u.approval_status,
                        version=u.version,
                        role_id=None,
                        scope_limit=(),
                    )
                )

        next_cursor = None
        if start_index + page_size < len(manageable_users) and page_users:
            pos = AdminUsersPagePosition(
                actor=principal.user_id,
                policy_version=decision.policy_version,
                status=status,
                page_size=page_size,
                after=page_users[-1].id,
            )
            next_cursor = codec._encode_chat_position(pos, _AdminUsersPagePayload)

        return {
            "items": items,
            "next_cursor": next_cursor,
            "policy_version": decision.policy_version,
        }

    async def list_roles(self, token, *, path, query, active, page_size, cursor, codec):
        principal, decision = await self.authorize(
            token, method="GET", path=path, query=query
        )
        if codec is None:
            raise CursorUnavailable()
        after = None
        if cursor:
            position = codec._decode_chat_position(cursor, _AdminRolesPagePayload)
            if (
                position.actor != principal.user_id
                or position.active != active
                or position.page_size != page_size
                or position.policy_version != decision.policy_version
            ):
                raise CursorRejected(
                    policy_changed=position.policy_version != decision.policy_version
                )
            after = position.after

        context = OwnAccessService._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            if await tx.get_policy_version() != decision.policy_version:
                raise CursorRejected(policy_changed=True)
            all_roles = await tx.access_management_roles()

            manageable_roles = []
            for r in all_roles:
                if active is not None and r.active != active:
                    continue
                if not (r.capabilities <= decision.snapshot.role.capabilities):
                    continue
                if not within_scope(r.grants, decision.snapshot.user.scope_limit):
                    continue
                manageable_roles.append(r)

        await self._authorization.recheck(decision)

        manageable_roles.sort(key=lambda r: str(r.id))

        start_index = 0
        if after is not None:
            after_str = str(after)
            for idx, r in enumerate(manageable_roles):
                if str(r.id) > after_str:
                    start_index = idx
                    break
            else:
                start_index = len(manageable_roles)

        page_roles = manageable_roles[start_index : start_index + page_size]

        items = []
        for r in page_roles:
            tokenized_grants = [
                _grant_to_wire(g, principal, decision.policy_version, codec)
                for g in r.grants
            ]
            items.append(
                AdminRoleItem(
                    id=r.id,
                    name_key=r.name_key,
                    active=r.active,
                    version=r.version,
                    capabilities=tuple(sorted(r.capabilities)),
                    grants=tuple(tokenized_grants),
                )
            )

        next_cursor = None
        if start_index + page_size < len(manageable_roles) and page_roles:
            pos = AdminRolesPagePosition(
                actor=principal.user_id,
                policy_version=decision.policy_version,
                active=active,
                page_size=page_size,
                after=page_roles[-1].id,
            )
            next_cursor = codec._encode_chat_position(pos, _AdminRolesPagePayload)

        return {
            "items": items,
            "next_cursor": next_cursor,
            "policy_version": decision.policy_version,
        }

    async def create_role(self, token, *, path, body, request_id, data, codec):
        principal, decision = await self.authorize(
            token, method="POST", path=path, body=body
        )
        grants = _resolve_scope(data.grants, principal, decision.policy_version, codec)
        now = utc_datetime(self._clock())
        new_role = RoleRecord(
            id=uuid4(),
            version=0,
            name_key=data.name_key,
            active=data.active,
            grants=grants,
            capabilities=frozenset(data.capabilities),
            created_at=now,
            updated_at=now,
        )
        created = await self._management.create_role(
            principal,
            new_role,
            expected_policy_version=data.expected_policy_version,
            audit_request_id=request_id,
        )
        context = OwnAccessService._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            committed_policy_version = await tx.get_policy_version()
        return {
            "id": created.id,
            "version": created.version,
            "policy_version": committed_policy_version,
        }

    async def update_role(self, token, *, path, body, request_id, role_id, data, codec):
        principal, decision = await self.authorize(
            token, method="PATCH", path=path, body=body
        )
        context = OwnAccessService._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            target_role = await tx.get_role(role_id)

        if target_role is None:
            raise ApiProblem(ErrorCode.NOT_FOUND)
        if target_role.id == decision.snapshot.role.id:
            raise AccessDenied("Access is not allowed.")
        if not (
            target_role.capabilities <= decision.snapshot.role.capabilities
        ) or not within_scope(target_role.grants, decision.snapshot.user.scope_limit):
            raise ApiProblem(ErrorCode.NOT_FOUND)

        if target_role.version != data.expected_version:
            raise AppWriteConflict("Role version changed.")

        grants = _resolve_scope(data.grants, principal, decision.policy_version, codec)
        now = utc_datetime(self._clock())
        updated_role = RoleRecord(
            id=role_id,
            version=data.expected_version + 1,
            name_key=data.name_key,
            active=data.active,
            grants=grants,
            capabilities=frozenset(data.capabilities),
            created_at=target_role.created_at,
            updated_at=now,
        )
        updated = await self._management.update_role(
            principal,
            updated_role,
            expected_version=data.expected_version,
            expected_policy_version=data.expected_policy_version,
            audit_request_id=request_id,
        )
        async with self._db.snapshot_transaction(context) as tx:
            committed_policy_version = await tx.get_policy_version()
        return {
            "id": updated.id,
            "version": updated.version,
            "policy_version": committed_policy_version,
        }

    async def change_user_access(
        self, token, *, path, body, request_id, user_id, data, codec
    ):
        principal, decision = await self.authorize(
            token, method="PATCH", path=path, body=body
        )
        if user_id == principal.user_id:
            raise AccessDenied("Access is not allowed.")

        context = OwnAccessService._context(token)
        async with self._db.snapshot_transaction(context) as tx:
            target_user = await tx.get_user(user_id)
            target_role = (
                await tx.get_role(target_user.role_id)
                if target_user and target_user.role_id
                else None
            )

        if target_user is None or target_user.approval_status != "approved":
            raise ApiProblem(ErrorCode.NOT_FOUND)
        if target_role is None or not (
            target_role.capabilities <= decision.snapshot.role.capabilities
        ):
            raise ApiProblem(ErrorCode.NOT_FOUND)
        if not within_scope(
            target_user.scope_limit, decision.snapshot.user.scope_limit
        ):
            raise ApiProblem(ErrorCode.NOT_FOUND)

        if target_user.version != data.expected_version:
            raise AppWriteConflict("User version changed.")

        scope = _resolve_scope(
            data.scope_limit, principal, decision.policy_version, codec
        )
        updated = await self._management.change_user(
            principal,
            user_id,
            role_id=data.role_id,
            scope=scope,
            status=data.approval_status,
            expected_version=data.expected_version,
            expected_policy_version=data.expected_policy_version,
            audit_request_id=request_id,
        )
        async with self._db.snapshot_transaction(context) as tx:
            committed_policy_version = await tx.get_policy_version()
        return {
            "id": updated.id,
            "approval_status": updated.approval_status,
            "version": updated.version,
            "policy_version": committed_policy_version,
        }
