"""Current, fail-closed RBAC/ABAC decisions for trusted backend callers.

These objects are internal contracts, never request bodies or model arguments.
Consumers apply every returned constraint before reads/aggregation, and recheck
before commit/release. Commit also uses the existing transactional policy fence.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from goodshowroom_ai.storage.access_requests import scope_within
from goodshowroom_ai.storage.identities import (
    RoleRecord,
    SourceReference,
    UserRecord,
)
from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.models import utc_datetime
from goodshowroom_ai.storage.settings import AppSetting, IdentityMapping


class AccessDenied(PermissionError):
    """Safe denial; never reveal record existence or policy internals."""


def deny() -> None:
    raise AccessDenied("Access is not allowed.")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Principal(Contract):
    """Identity from the verified internal assertion; contains no role claims."""

    user_id: UUID
    login_id: UUID
    generation: int = Field(ge=0)


class PolicySnapshot(Contract):
    """One consistent app-repository read; never built from browser input."""

    policy_version: int = Field(ge=0)
    user: UserRecord
    role: RoleRecord
    login: LoginRecord
    mappings: tuple[IdentityMapping, ...] = Field(default=(), max_length=100)
    sites: AppSetting | None = None


class CollectionPolicy(Contract):
    """Trusted adapter allowlist. An absent collection is unavailable."""

    database: Literal["byd-leads-new", "byd-panel"]
    collection: str
    fields: frozenset[str]
    tools: frozenset[str]
    metrics: frozenset[str] = frozenset()


class RecordAttributes(Contract):
    """Adapter-verified attributes, including for each joined record."""

    reference: SourceReference
    site: str
    assigned_to: tuple[SourceReference, ...] = ()


class DataAccess(Contract):
    database: Literal["byd-leads-new", "byd-panel"]
    collection: str
    site: str
    fields: frozenset[str] = Field(min_length=1, max_length=100)
    tool: str
    metric: str | None = None
    sensitive_breakdown: bool = False
    records: tuple[RecordAttributes, ...] = Field(default=(), max_length=100)


class ProtectedAction(Contract):
    action: Literal[
        "tool",
        "chat",
        "history",
        "delete_chat",
        "review_saved_chats",
        "manage_access",
        "manage_settings",
    ]
    owner_id: UUID | None = None
    mode: Literal["saved", "temporary"] | None = None
    chat_login_id: UUID | None = None
    data: tuple[DataAccess, ...] = Field(default=(), max_length=100)


class QueryConstraint(Contract):
    """OR these clauses; AND site, fields, tool and metric within each clause."""

    access: DataAccess
    record_mode: Literal["assigned", "selected", "all_permitted"]
    selected_records: tuple[SourceReference, ...] = ()
    assigned_to: tuple[SourceReference, ...] = ()


class AuthorizationDecision(Contract):
    principal: Principal
    request: ProtectedAction
    policy_version: int
    # Exact typed state comparison also catches a forgotten version increment.
    snapshot: PolicySnapshot = Field(repr=False)
    constraints: tuple[tuple[QueryConstraint, ...], ...]
    requires_review_audit: bool = False


PolicyLoader = Callable[[Principal], Awaitable[PolicySnapshot]]
IdentityCheck = Callable[[Principal], Awaitable[bool]]


class AuthorizationService:
    def __init__(
        self,
        *,
        load_policy: PolicyLoader,
        check_identity: IdentityCheck,
        clock: Callable[[], datetime],
        collections: tuple[CollectionPolicy, ...] = (),
    ):
        self._load_policy = load_policy
        self._check_identity = check_identity
        self._clock = clock
        self._collections = collections
        keys = [(c.database, c.collection) for c in collections]
        if len(keys) != len(set(keys)):
            raise ValueError("Ambiguous collection policy")

    async def authorize(
        self, principal: Principal, request: ProtectedAction
    ) -> AuthorizationDecision:
        try:
            if not isinstance(principal, Principal) or not isinstance(
                request, ProtectedAction
            ):
                deny()
            if await self._check_identity(principal) is not True:
                deny()
            policy = await self._load_policy(principal)
            now = utc_datetime(self._clock())
            if not isinstance(policy, PolicySnapshot):
                deny()
            user, role, login = policy.user, policy.role, policy.login
            if (
                user.id != principal.user_id
                or user.approval_status != "approved"
                or user.role_id != role.id
                or not role.active
                or login.id != principal.login_id
                or login.user_id != user.id
                or login.generation != principal.generation
                or not login.valid(now)
            ):
                deny()
            # Each user scope must fit a single role grant: never union separate grants.
            if not user.scope_limit or any(
                not any(scope_within(scope, grant) for grant in role.grants)
                for scope in user.scope_limit
            ):
                deny()
            self._action(principal, request, role)
            constraints = tuple(
                self._data(access, policy, now) for access in request.data
            )
            # Covers revocation/key change while the repository was awaited.
            if await self._check_identity(principal) is not True:
                deny()
            if not login.valid(utc_datetime(self._clock())):
                deny()
            return AuthorizationDecision(
                principal=principal,
                request=request,
                policy_version=policy.policy_version,
                snapshot=policy,
                constraints=constraints,
                requires_review_audit=request.action == "review_saved_chats",
            )
        except AccessDenied:
            raise
        except Exception:
            raise AccessDenied("Access is not allowed.") from None

    async def recheck(self, decision: AuthorizationDecision) -> AuthorizationDecision:
        """Call at tools, history, final commit and release, without policy caching."""
        if not isinstance(decision, AuthorizationDecision):
            deny()
        current = await self.authorize(decision.principal, decision.request)
        if (
            current.snapshot != decision.snapshot
            or current.constraints != decision.constraints
        ):
            deny()
        return current

    @staticmethod
    def _action(
        principal: Principal, request: ProtectedAction, role: RoleRecord
    ) -> None:
        action = request.action
        capability = (
            "chat" if action in {"tool", "chat", "history", "delete_chat"} else action
        )
        if capability not in role.capabilities:
            deny()
        if action == "tool":
            if not request.data:
                deny()
            if any(
                value is not None
                for value in (request.owner_id, request.mode, request.chat_login_id)
            ) and (
                request.owner_id != principal.user_id
                or request.mode is None
                or request.mode == "temporary"
                and request.chat_login_id != principal.login_id
            ):
                deny()
        elif action in {"chat", "history", "delete_chat"}:
            if request.owner_id != principal.user_id or request.mode is None:
                deny()
            if request.mode == "temporary" and (
                request.chat_login_id != principal.login_id or action == "history"
            ):
                deny()
        elif action == "review_saved_chats":
            if request.mode != "saved" or request.owner_id is None:
                deny()

    def _data(
        self, access: DataAccess, policy: PolicySnapshot, now: datetime
    ) -> tuple[QueryConstraint, ...]:
        allowed = next(
            (
                c
                for c in self._collections
                if (c.database, c.collection) == (access.database, access.collection)
            ),
            None,
        )
        if (
            allowed is None
            or not access.fields <= allowed.fields
            or access.tool not in allowed.tools
            or access.metric is not None
            and access.metric not in allowed.metrics
            or access.sensitive_breakdown
        ):
            # No approved disclosure/differencing rule exists yet.
            deny()
        if (
            policy.sites is None
            or policy.sites.id != "sites"
            or access.site not in {s.site_id for s in policy.sites.sites}
        ):
            deny()
        clauses = []
        for grant in policy.user.scope_limit:
            if (
                grant.dataset != access.database
                or access.site not in grant.sites
                or not access.fields <= set(grant.fields)
                or access.tool not in grant.tools
                or access.metric is not None
                and access.metric not in grant.metrics
            ):
                continue
            selected, assigned = (), ()
            if grant.record_mode == "selected":
                selected = tuple(
                    r
                    for r in grant.selected_records
                    if r.collection == access.collection
                )
                if not selected:
                    continue
            elif grant.record_mode == "assigned":
                mappings = tuple(
                    m
                    for m in policy.mappings
                    if m.active
                    and m.user_id == policy.user.id
                    and access.site in m.sites
                    and m.source_ref.database == access.database
                    and m.verified_at <= now
                )
                assigned = tuple(m.source_ref for m in mappings)
                if not assigned or len(set(assigned)) != len(assigned):
                    continue
            clauses.append(
                QueryConstraint(
                    access=access,
                    record_mode=grant.record_mode,
                    selected_records=selected,
                    assigned_to=assigned,
                )
            )
        if not clauses:
            deny()
        for record in access.records:
            if (
                record.reference.database != access.database
                or record.reference.collection != access.collection
                or record.site != access.site
                or not any(self._record(record, c) for c in clauses)
            ):
                deny()
        return tuple(clauses)

    @staticmethod
    def _record(record: RecordAttributes, constraint: QueryConstraint) -> bool:
        if constraint.record_mode == "selected":
            return record.reference in constraint.selected_records
        if constraint.record_mode == "assigned":
            return bool(set(record.assigned_to) & set(constraint.assigned_to))
        return True
