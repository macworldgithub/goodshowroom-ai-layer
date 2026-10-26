"""Single-process temporary context. No persistent repository or SDK session."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from goodshowroom_ai.agent_context import AgentHistory
from goodshowroom_ai.api_contracts import ErrorCode
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    Contract,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.responses import BuiltResponse, ResponseContext, ResponseInvalid
from goodshowroom_ai.storage.chat_sessions import ChatQuestionState
from goodshowroom_ai.storage.chat_turns import SavedTurnView, TurnCommand
from goodshowroom_ai.storage.models import utc_datetime
from goodshowroom_ai.storage.logins import LoginRecord

IDLE_LIMIT = timedelta(minutes=30)
SWEEP_SECONDS = 60
_logger = logging.getLogger(__name__)


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class TemporaryUnavailable(ApiProblem):
    def __init__(self):
        super().__init__(ErrorCode.TEMPORARY_UNAVAILABLE)


class TemporaryChat(Contract):
    id: UUID
    version: int = 0
    mode: Literal["temporary"] = "temporary"


class TemporaryAttempt(Contract):
    status: Literal["started", "committed"]
    built: BuiltResponse | None = None


@dataclass(slots=True, repr=False)
class _Pending:
    request_id: UUID
    request_hash: str
    task: asyncio.Task


@dataclass(slots=True, repr=False)
class _Entry:
    id: UUID
    principal: Principal
    creation_request_id: UUID
    last_activity: datetime
    valid_until: datetime
    version: int = 0
    turns: list[SavedTurnView] = field(default_factory=list)
    state: ChatQuestionState | None = None
    completed: dict[UUID, tuple[str, BuiltResponse]] = field(default_factory=dict)
    request_hashes: dict[UUID, str] = field(default_factory=dict)
    pending: _Pending | None = None


class TemporaryMemoryStore:
    def __init__(
        self,
        *,
        authorization: AuthorizationService,
        clock: Callable[[], datetime],
        max_chats: int = 256,
    ):
        if not callable(clock) or type(max_chats) is not int or max_chats < 1:
            raise ValueError("Invalid temporary storage configuration")
        self._authorization, self._clock, self._max_chats = (
            authorization,
            clock,
            max_chats,
        )
        self._entries: dict[UUID, _Entry] = {}
        self._creations: dict[tuple, UUID] = {}
        self._revoked: dict[tuple, datetime] = {}
        self._closed = False
        self._sweeper: asyncio.Task | None = None

    @staticmethod
    def _login_key(principal):
        if not isinstance(principal, Principal):
            raise AccessDenied("Access is not allowed.")
        return principal.user_id, principal.login_id, principal.generation

    def _now(self):
        return utc_datetime(self._clock())

    def _drop(self, identifier):
        entry = self._entries.pop(identifier, None)
        if entry is None:
            return
        self._creations.pop(
            (*self._login_key(entry.principal), entry.creation_request_id), None
        )
        task = entry.pending.task if entry.pending is not None else None
        entry.pending, entry.state = None, None
        entry.turns.clear()
        entry.completed.clear()
        entry.request_hashes.clear()
        if task is not None and not task.done() and task is not _current_task():
            task.cancel()

    async def _authorize(self, principal, chat_id):
        if self._closed:
            raise TemporaryUnavailable()
        if self._login_key(principal) in self._revoked:
            raise TemporaryUnavailable()
        try:
            decision = await self._authorization.authorize(
                principal,
                ProtectedAction(
                    action="chat",
                    owner_id=principal.user_id,
                    mode="temporary",
                    chat_login_id=principal.login_id,
                ),
            )
        except AccessDenied:
            for identifier, entry in tuple(self._entries.items()):
                if entry.principal == principal:
                    self._drop(identifier)
            raise
        if self._closed or self._login_key(principal) in self._revoked:
            raise TemporaryUnavailable()
        try:
            login = LoginRecord.model_validate_json(
                decision.snapshot.login.model_dump_json(warnings="error")
            )
        except Exception:
            raise AccessDenied("Access is not allowed.") from None
        now = self._now()
        if login.updated_at > now or not login.valid(now):
            raise AccessDenied("Access is not allowed.")
        return decision

    def _entry(self, principal, context, command):
        now = self._now()
        if self._closed or self._login_key(principal) in self._revoked:
            raise TemporaryUnavailable()
        if (
            not isinstance(context, ResponseContext)
            or context.mode != "temporary"
            or context.owner_id != principal.user_id
            or context.chat_login_id != principal.login_id
        ):
            raise TemporaryUnavailable()
        entry = self._entries.get(context.session_id)
        if entry is None or entry.principal != principal:
            raise TemporaryUnavailable()
        if now >= entry.valid_until or now >= entry.last_activity + IDLE_LIMIT:
            self._drop(entry.id)
            raise TemporaryUnavailable()
        if (
            not isinstance(command, TurnCommand)
            or command.request_id != context.request_id
        ):
            raise TemporaryUnavailable()
        return entry

    def _touch(self, entry, decision):
        login = decision.snapshot.login
        entry.last_activity = self._now()
        entry.valid_until = min(
            login.expires_at, login.auth_expires_at, login.lease_valid_until
        )

    async def create(self, principal: Principal, request_id: UUID) -> TemporaryChat:
        if type(request_id) is not UUID:
            raise TemporaryUnavailable()
        decision = await self._authorize(principal, None)
        self.sweep()
        key = (*self._login_key(principal), request_id)
        identifier = self._creations.get(key)
        if identifier is not None:
            entry = self._entries.get(identifier)
            if entry is not None:
                return TemporaryChat(id=identifier, version=entry.version)
        if self._closed or len(self._entries) >= self._max_chats:
            raise TemporaryUnavailable()
        now = self._now()
        login = decision.snapshot.login
        entry = _Entry(
            uuid4(),
            principal,
            request_id,
            now,
            min(login.expires_at, login.auth_expires_at, login.lease_valid_until),
        )
        self._entries[entry.id], self._creations[key] = entry, entry.id
        return TemporaryChat(id=entry.id)

    async def begin_turn(self, principal, context, command) -> TemporaryAttempt:
        decision = await self._authorize(principal, context.session_id)
        entry = self._entry(principal, context, command)
        digest = command.request_hash(principal.user_id, context.session_id)
        previous_hash = entry.request_hashes.get(command.request_id)
        if previous_hash is not None and previous_hash != digest:
            raise ApiProblem(ErrorCode.IDEMPOTENCY_CONFLICT)
        cached = entry.completed.get(command.request_id)
        if cached is not None:
            if cached[0] != digest:
                raise ApiProblem(ErrorCode.IDEMPOTENCY_CONFLICT)
            if cached[1].response.policy_version != decision.policy_version:
                raise AccessDenied("Access is not allowed.")
            self._touch(entry, decision)
            return TemporaryAttempt(status="committed", built=cached[1])
        if entry.pending is not None:
            if (
                entry.pending.request_id == command.request_id
                and entry.pending.request_hash != digest
            ):
                raise ApiProblem(ErrorCode.IDEMPOTENCY_CONFLICT)
            raise ApiProblem(ErrorCode.REQUEST_IN_PROGRESS)
        if entry.version != command.expected_version:
            raise ApiProblem(ErrorCode.VERSION_CONFLICT)
        if (
            command.request_id not in entry.request_hashes
            and len(entry.request_hashes) >= 1024
        ):
            self._drop(entry.id)
            raise TemporaryUnavailable()
        entry.request_hashes[command.request_id] = digest
        entry.pending = _Pending(command.request_id, digest, asyncio.current_task())
        self._touch(entry, decision)
        return TemporaryAttempt(status="started")

    def _require_pending(self, entry, command):
        pending = entry.pending
        if (
            pending is None
            or pending.task is not asyncio.current_task()
            or pending.request_id != command.request_id
            or pending.request_hash
            != command.request_hash(entry.principal.user_id, entry.id)
        ):
            raise TemporaryUnavailable()
        if entry.version != command.expected_version:
            raise ApiProblem(ErrorCode.VERSION_CONFLICT)

    async def load(self, principal, context, command) -> AgentHistory:
        decision = await self._authorize(principal, context.session_id)
        entry = self._entry(principal, context, command)
        self._require_pending(entry, command)
        self._touch(entry, decision)
        return AgentHistory(
            owner_id=principal.user_id,
            session_id=entry.id,
            mode="temporary",
            login_id=principal.login_id,
            version=entry.version,
            policy_version=decision.policy_version,
            turns=tuple(entry.turns),
            state=entry.state,
        )

    async def commit_completed_turn(self, principal, context, command, built):
        decision = await self._authorize(principal, context.session_id)
        entry = self._entry(principal, context, command)
        self._require_pending(entry, command)
        if (
            not isinstance(built, BuiltResponse)
            or built.request_id != command.request_id
            or built.response.policy_version != decision.policy_version
        ):
            raise ResponseInvalid("Response is invalid.")
        built = BuiltResponse.model_validate_json(
            built.model_dump_json(warnings="error")
        )
        sequence = entry.version + 1
        state = (
            entry.state
            if entry.state is not None
            and entry.state.policy_version == decision.policy_version
            else None
        )
        if built.response.status == "complete":
            response = built.response
            state = ChatQuestionState(
                site=response.scope.site,
                metric_version=response.facts[0].metric_version,
                start_utc=response.period.start_utc,
                end_utc=response.period.end_utc,
                timezone=response.period.timezone,
                policy_version=decision.policy_version,
                through_sequence=sequence,
            )
        turn = SavedTurnView(
            id=uuid4(),
            sequence=sequence,
            user_message=command.message,
            response=built.response,
            committed_at=self._now(),
        )
        # No await between validation and update: one event-loop atomic memory turn.
        entry.turns = (entry.turns + [turn])[-20:]
        entry.state, entry.version = state, sequence
        entry.completed[command.request_id] = (
            command.request_hash(principal.user_id, entry.id),
            built,
        )
        while len(entry.completed) > 32:
            entry.completed.pop(next(iter(entry.completed)))
        self._touch(entry, decision)

    async def validate_release(self, principal, context, command):
        decision = await self._authorize(principal, context.session_id)
        entry = self._entry(principal, context, command)
        cached = entry.completed.get(command.request_id)
        if (
            cached is None
            or cached[0] != command.request_hash(principal.user_id, entry.id)
            or cached[1].response.policy_version != decision.policy_version
        ):
            raise TemporaryUnavailable()

    async def finish_turn(self, principal, context, command):
        entry = self._entries.get(context.session_id)
        if (
            entry is not None
            and entry.principal == principal
            and entry.pending is not None
            and entry.pending.task is asyncio.current_task()
            and entry.pending.request_id == command.request_id
        ):
            entry.pending = None

    async def request_status(
        self, principal, context, command
    ) -> Literal["committed", "processing", "retry_allowed", "unknown"]:
        await self._authorize(principal, context.session_id)
        entry = self._entry(principal, context, command)
        digest = command.request_hash(principal.user_id, entry.id)
        previous_hash = entry.request_hashes.get(command.request_id)
        if previous_hash is not None and previous_hash != digest:
            raise ApiProblem(ErrorCode.IDEMPOTENCY_CONFLICT)
        cached = entry.completed.get(command.request_id)
        if cached is not None:
            if cached[0] != digest:
                raise ApiProblem(ErrorCode.IDEMPOTENCY_CONFLICT)
            return "committed"  # No transcript returned through this status method.
        if entry.pending is not None and entry.pending.request_id == command.request_id:
            if (
                entry.pending.request_id == command.request_id
                and entry.pending.request_hash != digest
            ):
                raise ApiProblem(ErrorCode.IDEMPOTENCY_CONFLICT)
            return "processing"
        if previous_hash is not None and command.expected_version < entry.version:
            return "unknown"  # Its reply aged out; never silently rerun it.
        if command.expected_version != entry.version:
            raise ApiProblem(ErrorCode.VERSION_CONFLICT)
        return "retry_allowed"

    async def end(self, principal, chat_id: UUID):
        await self._authorize(principal, chat_id)
        entry = self._entries.get(chat_id)
        if entry is not None and entry.principal == principal:
            self._drop(chat_id)

    async def invalidate_login(self, principal):
        """Trusted verified logout callback, including already revoked logins.

        It exposes no contents and accepts no browser-provided role/owner claim.
        Route identity validation is owned by the signed-login integration.
        """
        key = self._login_key(principal)
        self._revoked[key] = self._now() + timedelta(hours=8)
        for identifier, entry in tuple(self._entries.items()):
            if entry.principal == principal:
                self._drop(identifier)

    async def refresh_login(self, principal):
        decision = await self._authorize(principal, None)
        self.sweep()  # Fresh verification never revives context whose lease expired.
        login = decision.snapshot.login
        for entry in self._entries.values():
            if entry.principal == principal:
                entry.valid_until = min(
                    login.expires_at, login.auth_expires_at, login.lease_valid_until
                )

    def sweep(self):
        now = self._now()
        for key, expiry in tuple(self._revoked.items()):
            if expiry <= now:
                self._revoked.pop(key, None)
        for identifier, entry in tuple(self._entries.items()):
            if now >= entry.valid_until or now >= entry.last_activity + IDLE_LIMIT:
                self._drop(identifier)

    async def _sweep_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(SWEEP_SECONDS)
                self.sweep()
        except asyncio.CancelledError:
            return
        except Exception:
            _logger.warning("temporary_cleanup_failed")
            self._closed = True
            for identifier in tuple(self._entries):
                self._drop(identifier)

    async def start(self):
        if self._closed:
            raise TemporaryUnavailable()
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def close(self):
        self._closed = True
        for identifier in tuple(self._entries):
            self._drop(identifier)
        self._revoked.clear()
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None


class LogoutResult(Contract):
    backend_invalidated: bool
    cleanup_confirmed: bool


class LogoutCoordinator:
    def __init__(
        self,
        *,
        memory: TemporaryMemoryStore,
        revoke_login: Callable[[Principal], Awaitable[None]],
    ):
        self._memory, self._revoke = memory, revoke_login

    async def logout(self, principal: Principal) -> LogoutResult:
        # Drop local context even if the metadata revocation transport later fails.
        await self._memory.invalidate_login(principal)
        try:
            await self._revoke(principal)
        except Exception:
            return LogoutResult(backend_invalidated=False, cleanup_confirmed=False)
        return LogoutResult(backend_invalidated=True, cleanup_confirmed=True)
