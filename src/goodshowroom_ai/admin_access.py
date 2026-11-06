"""Identity boundary for administrator access-management operations."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from goodshowroom_ai.access_management import AccessManagement
from goodshowroom_ai.api_contracts import InvalidContract
from goodshowroom_ai.authorization import Contract, Principal, ProtectedAction
from goodshowroom_ai.cursors import CursorRejected, CursorUnavailable
from goodshowroom_ai.own_access import OwnAccessService
from goodshowroom_ai.storage.access_requests import AccessRequestRecord, scope_within
from goodshowroom_ai.storage.identities import Grant, SourceReference
from goodshowroom_ai.storage.models import utc_datetime


class AccessPagePosition(Contract):
    actor: UUID
    policy_version: int = Field(ge=0)
    status: Literal["pending", "approved", "rejected"]
    page_size: int = Field(ge=1, le=100)
    after: UUID


class _AccessPagePayload(Contract):
    purpose: Literal["goodshowroom.access-requests.v1"] = (
        "goodshowroom.access-requests.v1"
    )
    position: AccessPagePosition


class RecordHandlePosition(Contract):
    actor: UUID
    policy_version: int = Field(ge=0)
    source: SourceReference


class _RecordHandlePayload(Contract):
    purpose: Literal["goodshowroom.access-record.v1"] = "goodshowroom.access-record.v1"
    position: RecordHandlePosition


class AdminAccess:
    """Verify the exact operation before checking the current administrator role."""

    def __init__(self, db, verifier, authorization, clock, workflow=None):
        self._db = db
        self._verifier = verifier
        self._authorization = authorization
        self._clock = clock
        self._workflow = workflow
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

    async def issue_known_record_handle(self, principal, source, codec):
        """Seal a record already present in the administrator's selected scope."""
        if codec is None:
            raise CursorUnavailable()
        decision = await self._authorization.authorize(
            principal, ProtectedAction(action="manage_access")
        )
        if not isinstance(source, SourceReference) or not any(
            scope.record_mode == "selected" and source in scope.selected_records
            for scope in decision.snapshot.user.scope_limit
        ):
            raise CursorRejected()
        await self._authorization.recheck(decision)
        position = RecordHandlePosition(
            actor=principal.user_id,
            policy_version=decision.policy_version,
            source=source,
        )
        return codec._encode_chat_position(position, _RecordHandlePayload)

    def _resolve_scope(self, scope, principal, version, codec):
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
                    raise CursorRejected(
                        policy_changed=position.policy_version != version
                    )
                selected.append(position.source)
            values["selected_records"] = tuple(selected)
            try:
                result.append(Grant.model_validate(values))
            except ValueError:
                raise CursorRejected() from None
        return tuple(result)

    async def change(
        self, token, *, path, body, request_id, data, target=None, codec=None
    ):
        principal, decision = await self.authorize(
            token, method="POST", path=path, body=body
        )
        scope = self._resolve_scope(
            getattr(data, "scope_limit", ()), principal, decision.policy_version, codec
        )
        options = {
            "expected_policy_version": data.expected_policy_version,
            "audit_request_id": request_id,
        }
        if path == "/v1/admin/preapprovals":
            now = utc_datetime(self._clock())
            if data.expires_at is not None and data.expires_at <= now:
                raise InvalidContract("Request is invalid.")
            record = AccessRequestRecord(
                id=uuid4(),
                kind="preapproval",
                email_key=data.email,
                role_id=data.role_id,
                scope_limit=scope,
                expires_at=data.expires_at,
                created_at=now,
                updated_at=now,
            )
            result = await self._management.create_preapproval(
                principal, record, **options
            )
            identifier, status = result.id, "pending"
        elif path == f"/v1/admin/preapprovals/{target}/revoke":
            await self._management.revoke_preapproval(principal, target, **options)
            identifier, status = target, "revoked"
        elif path == f"/v1/admin/access-requests/{target}/decision":
            await self._workflow.decide(
                principal,
                target,
                approve=data.decision == "approve",
                role_id=data.role_id,
                scope=scope,
                **options,
            )
            identifier = target
            status = "approved" if data.decision == "approve" else "rejected"
        else:
            raise ValueError("Unsupported operation")
        current_principal, decision = await self.authorize(
            token, method="POST", path=path, body=body
        )
        if current_principal != principal:
            raise PermissionError("Access denied")
        return {
            "id": identifier,
            "status": status,
            "policy_version": decision.policy_version,
        }

    async def list_requests(
        self, token, *, path, query, status, page_size, cursor, codec
    ):
        principal, decision = await self.authorize(
            token, method="GET", path=path, query=query
        )
        if codec is None:
            raise CursorUnavailable()
        after = None
        if cursor:
            position = codec._decode_chat_position(cursor, _AccessPagePayload)
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
            rows = await tx.list_access_requests(
                status=status, limit=page_size, after_id=after
            )
            items = []
            for record in rows:
                if record.kind != "request":
                    continue
                if record.scope_limit and any(
                    not any(
                        scope_within(scope, allowed)
                        for allowed in decision.snapshot.user.scope_limit
                    )
                    for scope in record.scope_limit
                ):
                    continue
                if record.role_id is not None:
                    role = await tx.get_role(record.role_id)
                    if (
                        role is None
                        or not role.capabilities <= decision.snapshot.role.capabilities
                    ):
                        continue
                items.append(
                    {
                        "id": record.id,
                        "user_id": record.user_id,
                        "email": record.email_key,
                        "status": record.status,
                        "created_at": record.created_at,
                    }
                )
        await self._authorization.recheck(decision)
        next_cursor = None
        if len(rows) == page_size:
            position = AccessPagePosition(
                actor=principal.user_id,
                policy_version=decision.policy_version,
                status=status,
                page_size=page_size,
                after=rows[-1].id,
            )
            next_cursor = codec._encode_chat_position(position, _AccessPagePayload)
        return {
            "items": items,
            "next_cursor": next_cursor,
            "policy_version": decision.policy_version,
        }
