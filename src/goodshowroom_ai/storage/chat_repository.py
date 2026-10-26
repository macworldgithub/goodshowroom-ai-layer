"""Authoritative owner-scoped persistence for saved-chat parent records."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from goodshowroom_ai.authorization import AuthorizationDecision
from goodshowroom_ai.cursors import CursorCodec, CursorUnavailable
from goodshowroom_ai.execution import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from goodshowroom_ai.storage.chat_sessions import (
    ChatReviewPosition,
    ChatSessionPage,
    ChatSessionPosition,
    ChatSessionRecord,
    ChatSessionSummary,
    LastTurnAttempt,
)
from goodshowroom_ai.storage.chat_turns import (
    ChatTurnPage,
    ChatTurnPosition,
    ChatTurnRecord,
    SavedTurnView,
    TurnAttemptResult,
    TurnCommand,
    TurnCommitResult,
    TurnRecoveryResult,
)
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    MAX_APP_DOCUMENT_BYTES,
    InternalAuthorizationContext,
    StorageCapability,
    utc_datetime,
    validate_bson_document,
)
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppDatabaseUnavailable,
    AppCommitOutcomeUnknown,
    AppWriteConflict,
    AuthorizationDenied,
    SavedChatCleanupPending,
)
from goodshowroom_ai.responses import BuiltResponse

_PROCESS_ID = uuid4()
_ACTIVE_RUNS: set[UUID] = set()
_MAX_DELETE_TURN_BATCHES_PER_REQUEST = 10


class ChatRepository:
    """Own saved-chat persistence, history reads, review and deletion."""

    def __init__(
        self,
        database: AppDatabase,
        cursor_codec: CursorCodec | None,
        *,
        clock: Callable[[], datetime] | None = None,
        authorization_recheck: (
            Callable[[AuthorizationDecision], Awaitable[AuthorizationDecision]] | None
        ) = None,
        turn_access_check: (
            Callable[[AuthorizationDecision, ChatTurnRecord], Awaitable[bool]] | None
        ) = None,
    ):
        if not isinstance(database, AppDatabase):
            raise ValueError("Chat repository requires the app database")
        if cursor_codec is not None and not isinstance(cursor_codec, CursorCodec):
            raise ValueError("Chat cursor configuration is invalid")
        self._database = database
        self._cursor_codec = cursor_codec
        self._clock = clock or (lambda: datetime.now(UTC))
        if authorization_recheck is not None and not callable(authorization_recheck):
            raise ValueError("Saved-chat authorization recheck is invalid")
        if turn_access_check is not None and not callable(turn_access_check):
            raise ValueError("Saved-turn authorization check is invalid")
        self._authorization_recheck = authorization_recheck
        self._turn_access_check = turn_access_check
        self._process_id = _PROCESS_ID
        self._active_runs = _ACTIVE_RUNS

    async def create_saved_chat(
        self,
        decision: AuthorizationDecision,
        request_id: UUID,
        *,
        mode: str = "saved",
    ) -> ChatSessionRecord:
        """Create or recover a session for this owner and request UUID only."""
        self._require_decision(decision, action="chat")
        if mode != "saved":
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        if type(request_id) is not UUID:
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        now = self._now()
        user_id = decision.principal.user_id
        context = self._context(user_id, write=True)
        try:
            async with self._database.transaction(context) as transaction:
                await transaction.require_chat_access(decision.policy_version)
                current = await transaction.find_chat_session_by_request(request_id)
                if current is not None:
                    return self._require_live_owned_record(current, user_id, now)
                record = ChatSessionRecord(
                    id=uuid4(),
                    owner_user_id=user_id,
                    creation_request_id=request_id,
                    title="New chat",
                    status="active",
                    version=0,
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
                await transaction.insert_chat_session(record)
                return record
        except AppWriteConflict as conflict:
            # A concurrent identical create may win the unique owner/request index.
            # Recover with a fresh read transaction; never replay the write blindly.
            recovered = await self._recover_creation(decision, request_id, user_id)
            if recovered is not None:
                return recovered
            raise conflict

    async def list_saved_chats(
        self,
        decision: AuthorizationDecision,
        *,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ChatSessionPage:
        """Return a bounded page of safe metadata for the current owner."""
        self._require_decision(decision, action="history")
        if (
            type(page_size) is not int
            or not 1 <= page_size <= MAX_PAGE_SIZE
            or (cursor is not None and type(cursor) is not str)
        ):
            raise AuthorizationDenied("Saved-chat page is not authorized.")
        user_id = decision.principal.user_id
        position = None
        if cursor is not None:
            if self._cursor_codec is None:
                raise CursorUnavailable()
            position = self._cursor_codec.decode_chat_page(
                cursor,
                owner_user_id=user_id,
                policy_version=decision.policy_version,
                page_size=page_size,
            )
        context = self._context(user_id, write=False)
        now = self._now()
        async with self._database.transaction(context) as transaction:
            await transaction.require_chat_access(decision.policy_version)
            records, has_more = await transaction.list_chat_sessions(
                now=now,
                page_size=page_size,
                after=position,
            )
        chats = tuple(
            ChatSessionSummary(
                id=record.id,
                title=record.title,
                version=record.version,
                last_activity_at=record.last_activity_at,
            )
            for record in records
        )
        size = sum(
            validate_bson_document(chat.model_dump(mode="json")) for chat in chats
        )
        if size > MAX_APP_DOCUMENT_BYTES:
            raise AppDatabaseUnavailable("Saved-chat page is unavailable.")
        next_cursor = None
        if has_more:
            if self._cursor_codec is None:
                raise CursorUnavailable()
            last = chats[-1]
            next_cursor = self._cursor_codec.encode_chat_page(
                ChatSessionPosition(
                    owner_user_id=user_id,
                    policy_version=decision.policy_version,
                    page_size=page_size,
                    last_activity_at=last.last_activity_at,
                    session_id=last.id,
                )
            )
        return ChatSessionPage(chats=chats, next_cursor=next_cursor)

    async def load_agent_history(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        expected_version: int,
    ):
        """Load the most recent bounded snapshot, never a permanent SDK session."""
        from goodshowroom_ai.agent_context import AgentHistory

        self._require_history_decision(decision)
        if (
            type(session_id) is not UUID
            or type(expected_version) is not int
            or expected_version < 0
        ):
            raise AuthorizationDenied("Conversation context is not authorized.")
        if self._turn_access_check is None:
            raise AuthorizationDenied("Saved-history authorization is unavailable.")
        owner = decision.principal.user_id
        now = self._now()
        async with self._database.snapshot_transaction(
            self._history_context(owner)
        ) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = self._require_live_owned_record(
                await transaction.find_chat_session(session_id),
                owner,
                now,
            )
            if parent.version != expected_version:
                raise AppWriteConflict("Saved-chat version changed.")
            records, _ = await transaction.list_chat_turns(
                session_id,
                snapshot_version=expected_version,
                after_sequence=max(0, expected_version - 20),
                page_size=20,
            )
        allowed = []
        state = None
        for record in records:
            if (
                not isinstance(record, ChatTurnRecord)
                or record.session_id != session_id
                or record.owner_user_id != owner
            ):
                raise AuthorizationDenied("Conversation context is not authorized.")
            try:
                permitted = await self._turn_access_check(decision, record)
            except Exception:
                raise AuthorizationDenied(
                    "Conversation context is not authorized."
                ) from None
            if type(permitted) is not bool:
                raise AuthorizationDenied("Conversation context is not authorized.")
            if not permitted:
                continue
            allowed.append(
                SavedTurnView(
                    id=record.id,
                    sequence=record.sequence,
                    user_message=record.user_message,
                    response=record.response,
                    committed_at=record.committed_at,
                )
            )
            candidate = record.question_state_after
            if (
                record.response.status == "complete"
                and candidate is not None
                and candidate.policy_version == decision.policy_version
                and candidate.through_sequence == record.sequence
                and record.response.scope is not None
                and candidate.site == record.response.scope.site
                and candidate.metric_version == record.response.facts[0].metric_version
            ):
                state = candidate
        await self._recheck_saved_history_decision(
            decision, action="history", owner_user_id=owner
        )
        return AgentHistory(
            owner_id=owner,
            session_id=session_id,
            mode="saved",
            version=expected_version,
            policy_version=decision.policy_version,
            turns=tuple(allowed),
            state=state,
        )

    async def list_saved_turns(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        *,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ChatTurnPage:
        """Return one owner's bounded page after current grants filter each turn."""
        self._require_history_decision(decision)
        return await self._read_saved_turns(
            decision,
            decision.principal.user_id,
            session_id,
            action="history",
            cursor=cursor,
            page_size=page_size,
        )

    async def review_saved_turns(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        *,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ChatTurnPage:
        """Read an owner's saved turns through a separate audited admin path."""
        self._require_review_decision(decision)
        owner_user_id = decision.request.owner_id
        if owner_user_id is None:
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        return await self._read_saved_turns(
            decision,
            owner_user_id,
            session_id,
            action="review_saved_chats",
            cursor=cursor,
            page_size=page_size,
        )

    async def review_saved_chats(
        self,
        decision: AuthorizationDecision,
        *,
        owner_user_id: UUID,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> ChatSessionPage:
        """List only one target owner's safe chat metadata and audit the review."""
        self._require_review_decision(decision, owner_user_id=owner_user_id)
        if (
            type(page_size) is not int
            or not 1 <= page_size <= MAX_PAGE_SIZE
            or (cursor is not None and type(cursor) is not str)
            or (since is None) != (until is None)
        ):
            raise AuthorizationDenied("Saved-chat review page is not authorized.")
        since = utc_datetime(since) if since is not None else None
        until = utc_datetime(until) if until is not None else None
        if since is not None and until is not None and since >= until:
            raise AuthorizationDenied("Saved-chat review page is not authorized.")
        actor_user_id = decision.principal.user_id
        position = None
        if cursor is not None:
            if self._cursor_codec is None:
                raise CursorUnavailable()
            position = self._cursor_codec.decode_chat_review_page(
                cursor,
                actor_user_id=actor_user_id,
                owner_user_id=owner_user_id,
                policy_version=decision.policy_version,
                page_size=page_size,
                since=since,
                until=until,
            )
        now = self._now()
        context = self._review_context(actor_user_id)
        async with self._database.snapshot_transaction(context) as transaction:
            await self._require_current_review_access(transaction, decision, now)
            records, has_more = await transaction.list_review_chat_sessions(
                owner_user_id,
                now=now,
                page_size=page_size,
                since=since,
                until=until,
                after=position,
                policy_version=decision.policy_version,
            )
            await transaction.record_saved_review_audit(
                self._review_audit_event(actor_user_id, owner_user_id, now),
                expected_policy_version=decision.policy_version,
            )
        chats = tuple(
            ChatSessionSummary(
                id=record.id,
                title=record.title,
                version=record.version,
                last_activity_at=record.last_activity_at,
            )
            for record in records
        )
        if sum(
            validate_bson_document(chat.model_dump(mode="json")) for chat in chats
        ) > (MAX_APP_DOCUMENT_BYTES):
            raise AppDatabaseUnavailable("Saved-chat review is unavailable.")
        next_cursor = None
        if has_more:
            if self._cursor_codec is None:
                raise CursorUnavailable()
            last = chats[-1]
            next_cursor = self._cursor_codec.encode_chat_review_page(
                ChatReviewPosition(
                    actor_user_id=actor_user_id,
                    owner_user_id=owner_user_id,
                    policy_version=decision.policy_version,
                    page_size=page_size,
                    last_activity_at=last.last_activity_at,
                    session_id=last.id,
                    since=since,
                    until=until,
                )
            )
        await self._recheck_saved_history_decision(
            decision, action="review_saved_chats", owner_user_id=owner_user_id
        )
        return ChatSessionPage(chats=chats, next_cursor=next_cursor)

    async def delete_saved_chat(
        self, decision: AuthorizationDecision, session_id: UUID
    ) -> None:
        """Mark a chat deleting, then idempotently remove its stored derivatives."""
        self._require_decision(decision, action="delete_chat")
        if type(session_id) is not UUID:
            raise AuthorizationDenied("Saved-chat deletion is not authorized.")
        owner_user_id = decision.principal.user_id
        await self._recheck_saved_history_decision(
            decision, action="delete_chat", owner_user_id=owner_user_id
        )
        context = self._deletion_context(owner_user_id)
        now = self._now()
        async with self._database.saved_turn_transaction(context) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = await transaction.mark_chat_deleting(session_id, now=now)
        if parent is None:
            return

        try:
            for _ in range(_MAX_DELETE_TURN_BATCHES_PER_REQUEST):
                await self._recheck_saved_history_decision(
                    decision, action="delete_chat", owner_user_id=owner_user_id
                )
                async with self._database.saved_turn_transaction(
                    context
                ) as transaction:
                    await self._require_current_turn_access(
                        transaction, decision, self._now()
                    )
                    more_turns = await transaction.delete_chat_turns(
                        session_id, batch_size=MAX_PAGE_SIZE
                    )
                if not more_turns:
                    break
            else:
                raise SavedChatCleanupPending()

            await self._recheck_saved_history_decision(
                decision, action="delete_chat", owner_user_id=owner_user_id
            )
            async with self._database.saved_turn_transaction(context) as transaction:
                await self._require_current_turn_access(
                    transaction, decision, self._now()
                )
                await transaction.delete_chat_session(session_id)
        except (AppDatabaseUnavailable, AppWriteConflict):
            raise SavedChatCleanupPending() from None

    async def _read_saved_turns(
        self,
        decision: AuthorizationDecision,
        owner_user_id: UUID,
        session_id: UUID,
        *,
        action: str,
        cursor: str | None,
        page_size: int,
    ) -> ChatTurnPage:
        if (
            type(owner_user_id) is not UUID
            or type(session_id) is not UUID
            or type(page_size) is not int
            or not 1 <= page_size <= MAX_PAGE_SIZE
            or (cursor is not None and type(cursor) is not str)
        ):
            raise AuthorizationDenied("Saved-turn page is not authorized.")
        if self._turn_access_check is None:
            raise AuthorizationDenied("Saved-history authorization is unavailable.")
        actor_user_id = decision.principal.user_id
        position = None
        if cursor is not None:
            if self._cursor_codec is None:
                raise CursorUnavailable()
            position = self._cursor_codec.decode_chat_turn_page(
                cursor,
                actor_user_id=actor_user_id,
                owner_user_id=owner_user_id,
                session_id=session_id,
                action=action,
                policy_version=decision.policy_version,
                page_size=page_size,
            )
        now = self._now()
        if action == "history":
            context = self._history_context(actor_user_id)
            async with self._database.snapshot_transaction(context) as transaction:
                await self._require_current_turn_access(transaction, decision, now)
                parent = self._require_live_owned_record(
                    await transaction.find_chat_session(session_id),
                    owner_user_id,
                    now,
                )
                snapshot_version = (
                    position.snapshot_version
                    if position is not None
                    else parent.version
                )
                if parent.version < snapshot_version:
                    raise AppDatabaseUnavailable("Saved-turn page is unavailable.")
                records, has_more = await transaction.list_chat_turns(
                    session_id,
                    snapshot_version=snapshot_version,
                    after_sequence=(
                        position.last_sequence if position is not None else 0
                    ),
                    page_size=page_size,
                )
        elif action == "review_saved_chats":
            context = self._review_context(actor_user_id)
            async with self._database.snapshot_transaction(context) as transaction:
                await self._require_current_review_access(transaction, decision, now)
                parent = await transaction.find_review_chat_session(
                    owner_user_id, session_id, now=now
                )
                self._require_live_review_record(parent, owner_user_id, now)
                snapshot_version = (
                    position.snapshot_version
                    if position is not None
                    else parent.version
                )
                if parent.version < snapshot_version:
                    raise AppDatabaseUnavailable("Saved-turn page is unavailable.")
                records, has_more = await transaction.list_review_chat_turns(
                    owner_user_id,
                    session_id,
                    snapshot_version=snapshot_version,
                    after_sequence=(
                        position.last_sequence if position is not None else 0
                    ),
                    page_size=page_size,
                )
                await transaction.record_saved_review_audit(
                    self._review_audit_event(actor_user_id, owner_user_id, now),
                    expected_policy_version=decision.policy_version,
                )
        else:
            raise AuthorizationDenied("Saved-turn page is not authorized.")

        snapshot_version = (
            position.snapshot_version if position is not None else parent.version
        )
        if parent.version < snapshot_version:
            raise AppDatabaseUnavailable("Saved-turn page is unavailable.")
        if any(record.expires_at != parent.expires_at for record in records):
            raise AppDatabaseUnavailable("Saved-turn page is unavailable.")
        allowed_turns: list[SavedTurnView] = []
        hidden = False
        for record in records:
            try:
                allowed = await self._turn_access_check(decision, record)
            except Exception:
                raise AuthorizationDenied(
                    "Saved-history authorization is unavailable."
                ) from None
            if type(allowed) is not bool:
                raise AuthorizationDenied("Saved-history authorization is unavailable.")
            if not allowed:
                hidden = True
                continue
            allowed_turns.append(
                SavedTurnView(
                    id=record.id,
                    sequence=record.sequence,
                    user_message=record.user_message,
                    response=record.response,
                    committed_at=record.committed_at,
                )
            )
        byte_size = sum(
            validate_bson_document(turn.model_dump(mode="json"))
            for turn in allowed_turns
        )
        if byte_size > MAX_APP_DOCUMENT_BYTES:
            raise AppDatabaseUnavailable("Saved-turn page is unavailable.")
        next_cursor = None
        if has_more:
            if self._cursor_codec is None or not records:
                raise CursorUnavailable()
            next_cursor = self._cursor_codec.encode_chat_turn_page(
                ChatTurnPosition(
                    actor_user_id=actor_user_id,
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    action=action,
                    policy_version=decision.policy_version,
                    page_size=page_size,
                    snapshot_version=snapshot_version,
                    last_sequence=records[-1].sequence,
                )
            )
        await self._recheck_saved_history_decision(
            decision, action=action, owner_user_id=owner_user_id
        )
        return ChatTurnPage(
            turns=tuple(allowed_turns),
            next_cursor=next_cursor,
            notice=(
                "Some saved messages are hidden because current access does not allow them."
                if hidden
                else None
            ),
        )

    async def begin_turn_attempt(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
    ) -> TurnAttemptResult:
        """Record content-free attempt metadata before model or source work."""
        decision = await self._recheck_turn_decision(decision, session_id)
        now = self._now()
        user_id = decision.principal.user_id
        request_hash = command.request_hash(user_id, session_id)
        run_id = uuid4()
        attempt = LastTurnAttempt(
            request_id=command.request_id,
            request_hash=request_hash,
            expected_version=command.expected_version,
            run_id=run_id,
            process_id=self._process_id,
            status="processing",
            started_at=now,
            updated_at=now,
        )
        self._active_runs.add(run_id)
        started = False
        try:
            async with self._database.saved_turn_transaction(
                self._turn_context(user_id)
            ) as transaction:
                await self._require_current_turn_access(transaction, decision, now)
                parent = await transaction.find_chat_session(session_id)
                self._require_live_owned_record(parent, user_id, now)
                existing = await transaction.find_chat_turn(
                    session_id, command.request_id
                )
                if existing is not None:
                    self._require_same_turn(existing, request_hash)
                    self._require_releasable_turn(decision, session_id, existing)
                    result = TurnAttemptResult(status="committed", turn=existing)
                else:
                    if parent.version != command.expected_version:
                        raise AppWriteConflict("Saved-chat version changed.")
                    previous = parent.last_attempt
                    if previous is not None:
                        if (
                            previous.request_id == command.request_id
                            and previous.request_hash != request_hash
                        ):
                            raise AppWriteConflict("Turn request payload changed.")
                        if previous.status == "unknown":
                            result = TurnAttemptResult(status="unknown")
                        elif previous.status == "processing":
                            if previous.run_id in self._active_runs:
                                result = TurnAttemptResult(
                                    status="processing", run_id=previous.run_id
                                )
                            elif previous.request_id == command.request_id:
                                stale_status = (
                                    "interrupted"
                                    if previous.process_id != self._process_id
                                    else "unknown"
                                )
                                terminal = self._attempt_copy(
                                    previous,
                                    status=stale_status,
                                    updated_at=now,
                                    error_category=(
                                        "cancelled"
                                        if stale_status == "interrupted"
                                        else None
                                    ),
                                )
                                await transaction.finish_chat_attempt(
                                    session_id, terminal
                                )
                                result = TurnAttemptResult(status=stale_status)
                            else:
                                raise AppWriteConflict(
                                    "A prior saved-turn attempt needs resolution."
                                )
                        elif (
                            previous.status == "committed"
                            and previous.request_id == command.request_id
                        ):
                            raise AppDatabaseUnavailable(
                                "Saved-turn state is inconsistent."
                            )
                        else:
                            await transaction.begin_chat_attempt(
                                session_id,
                                command.expected_version,
                                previous,
                                attempt,
                            )
                            result = TurnAttemptResult(status="started", run_id=run_id)
                            started = True
                    else:
                        await transaction.begin_chat_attempt(
                            session_id,
                            command.expected_version,
                            None,
                            attempt,
                        )
                        result = TurnAttemptResult(status="started", run_id=run_id)
                        started = True
            if result.status == "committed":
                await self._recheck_turn_decision(decision, session_id)
            return result
        except AppWriteConflict as conflict:
            recovered = await self._recover_attempt_race(
                decision, session_id, command, request_hash
            )
            if recovered is not None:
                return recovered
            raise conflict
        finally:
            if not started:
                self._active_runs.discard(run_id)

    async def fail_turn_attempt(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        run_id: UUID,
        error_category: str,
        *,
        interrupted: bool = False,
    ) -> None:
        try:
            await self._fail_turn_attempt(
                decision,
                session_id,
                command,
                run_id,
                error_category,
                interrupted=interrupted,
            )
        finally:
            if type(run_id) is UUID:
                self._active_runs.discard(run_id)

    async def _fail_turn_attempt(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        run_id: UUID,
        error_category: str,
        *,
        interrupted: bool = False,
    ) -> None:
        """Record a fixed failure category without storing errors or content."""
        decision = await self._recheck_turn_decision(decision, session_id)
        if error_category not in {
            "model_error",
            "validation_error",
            "timeout",
            "cancelled",
            "storage_error",
            "authorization_changed",
        }:
            raise ValueError("Saved-turn error category is invalid")
        now = self._now()
        user_id = decision.principal.user_id
        request_hash = command.request_hash(user_id, session_id)
        async with self._database.saved_turn_transaction(
            self._turn_context(user_id)
        ) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = await transaction.find_chat_session(session_id)
            self._require_live_owned_record(parent, user_id, now)
            current = parent.last_attempt
            if (
                current is None
                or current.run_id != run_id
                or current.request_id != command.request_id
                or current.request_hash != request_hash
                or current.status != "processing"
            ):
                raise AppWriteConflict("Saved-turn attempt changed.")
            updated = self._attempt_copy(
                current,
                status="interrupted" if interrupted else "failed",
                updated_at=now,
                error_category=error_category,
            )
            await transaction.finish_chat_attempt(session_id, updated)

    async def resolve_turn_request(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
    ) -> TurnRecoveryResult:
        """Reconcile saved-turn state; never rerun model work automatically."""
        decision = await self._recheck_turn_decision(decision, session_id)
        now = self._now()
        user_id = decision.principal.user_id
        request_hash = command.request_hash(user_id, session_id)
        recovered = None
        async with self._database.saved_turn_transaction(
            self._turn_context(user_id)
        ) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = await transaction.find_chat_session(session_id)
            self._require_live_owned_record(parent, user_id, now)
            existing = await transaction.find_chat_turn(session_id, command.request_id)
            if existing is not None:
                self._require_same_turn(existing, request_hash)
                self._require_releasable_turn(decision, session_id, existing)
                recovered = TurnRecoveryResult(status="committed", turn=existing)
            else:
                current = parent.last_attempt
                if current is not None and current.request_id == command.request_id:
                    if current.request_hash != request_hash:
                        raise AppWriteConflict("Turn request payload changed.")
                    if current.status == "unknown":
                        recovered = TurnRecoveryResult(status="unknown")
                    elif current.status == "processing":
                        if current.run_id in self._active_runs:
                            recovered = TurnRecoveryResult(status="processing")
                        else:
                            stale_status = (
                                "interrupted"
                                if current.process_id != self._process_id
                                else "unknown"
                            )
                            terminal = self._attempt_copy(
                                current,
                                status=stale_status,
                                updated_at=now,
                                error_category=(
                                    "cancelled"
                                    if stale_status == "interrupted"
                                    else None
                                ),
                            )
                            await transaction.finish_chat_attempt(session_id, terminal)
                            recovered = TurnRecoveryResult(
                                status=(
                                    "retry_allowed"
                                    if stale_status == "interrupted"
                                    else "unknown"
                                )
                            )
                    elif current.status in {"failed", "interrupted"}:
                        recovered = TurnRecoveryResult(status="retry_allowed")
                    else:
                        raise AppDatabaseUnavailable(
                            "Saved-turn state is inconsistent."
                        )
                elif parent.version == command.expected_version:
                    if current is not None and current.status in {
                        "processing",
                        "unknown",
                    }:
                        raise AppWriteConflict(
                            "A prior saved-turn attempt needs resolution."
                        )
                    recovered = TurnRecoveryResult(status="retry_allowed")
                else:
                    raise AppWriteConflict("Saved-chat version changed.")
        if recovered.status == "committed":
            await self._recheck_turn_decision(decision, session_id)
        return recovered

    async def commit_completed_turn(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        built_response: BuiltResponse,
        run_id: UUID,
    ) -> TurnCommitResult:
        try:
            return await self._commit_completed_turn(
                decision, session_id, command, built_response, run_id
            )
        finally:
            if type(run_id) is UUID:
                self._active_runs.discard(run_id)

    async def _commit_completed_turn(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        built_response: BuiltResponse,
        run_id: UUID,
    ) -> TurnCommitResult:
        """Commit only a complete typed response after current access checks."""
        decision = await self._recheck_turn_decision(decision, session_id)
        if (
            not isinstance(command, TurnCommand)
            or not isinstance(built_response, BuiltResponse)
            or type(run_id) is not UUID
            or built_response.request_id != command.request_id
            or built_response.response.policy_version != decision.policy_version
        ):
            raise AuthorizationDenied("Saved-turn response is not authorized.")
        now = self._now()
        user_id = decision.principal.user_id
        request_hash = command.request_hash(user_id, session_id)
        context = self._turn_context(user_id)
        try:
            async with self._database.saved_turn_transaction(context) as transaction:
                await self._require_current_turn_access(transaction, decision, now)
                current_parent = await transaction.find_chat_session(session_id)
                self._require_live_owned_record(current_parent, user_id, now)
                existing = await transaction.find_chat_turn(
                    session_id, command.request_id
                )
                if existing is not None:
                    self._require_same_turn(existing, request_hash)
                    self._require_releasable_turn(decision, session_id, existing)
                    result = TurnCommitResult(
                        status="committed", turn=existing, replayed=True
                    )
                else:
                    if current_parent.version != command.expected_version:
                        raise AppWriteConflict("Saved-chat version changed.")
                    self._require_active_attempt(
                        current_parent,
                        command,
                        request_hash,
                        run_id,
                        command.expected_version,
                    )
                    sequence = current_parent.version + 1
                    turn = ChatTurnRecord.completed(
                        session_id=session_id,
                        owner_user_id=user_id,
                        command=command,
                        built_response=built_response,
                        sequence=sequence,
                        committed_at=now,
                        expires_at=current_parent.expires_at,
                        previous_state=current_parent.context_state,
                    )
                    committed_attempt = self._attempt_copy(
                        current_parent.last_attempt,
                        status="committed",
                        updated_at=now,
                        error_category=None,
                    )
                    await transaction.advance_access_control_fence(
                        decision.policy_version
                    )
                    await transaction.commit_chat_turn(turn, committed_attempt)
                    await transaction.record_saved_turn_audit(
                        AppAuditEvent(
                            actor_id=user_id,
                            target_id=session_id,
                            action="saved_turn_commit",
                            outcome="success",
                            request_id=command.request_id,
                            created_at=now,
                        )
                    )
                    result = TurnCommitResult(status="committed", turn=turn)
        except AppCommitOutcomeUnknown:
            result = await self._reconcile_unknown_commit(
                decision, session_id, command, run_id, request_hash
            )
        except AppWriteConflict as conflict:
            result = await self._recover_committed_turn(
                decision, session_id, command, request_hash
            )
            if result is None:
                raise conflict
        finally:
            self._active_runs.discard(run_id)
        await self._recheck_turn_decision(decision, session_id)
        return result

    async def _recheck_turn_decision(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
    ) -> AuthorizationDecision:
        self._require_decision(decision, action="chat")
        if type(session_id) is not UUID or self._authorization_recheck is None:
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        try:
            current = await self._authorization_recheck(decision)
        except Exception:
            raise AuthorizationDenied("Saved-chat access is not authorized.") from None
        if (
            not isinstance(current, AuthorizationDecision)
            or current.principal != decision.principal
            or current.request != decision.request
            or current.policy_version != decision.policy_version
            or current.snapshot != decision.snapshot
            or current.constraints != decision.constraints
        ):
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        return current

    @staticmethod
    async def _require_current_turn_access(transaction, decision, now):
        await transaction.require_saved_turn_access(
            decision.policy_version,
            login_id=decision.principal.login_id,
            login_generation=decision.principal.generation,
            now=now,
        )

    @staticmethod
    def _turn_context(user_id: UUID) -> InternalAuthorizationContext:
        return InternalAuthorizationContext(
            actor_id=user_id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_IDENTITIES,
                    StorageCapability.READ_LOGINS,
                    StorageCapability.READ_POLICY,
                    StorageCapability.READ_CHATS,
                    StorageCapability.WRITE_CHATS,
                    StorageCapability.ADVANCE_ACCESS_CONTROL,
                    StorageCapability.WRITE_REQUIRED_AUDIT,
                }
            ),
        )

    @staticmethod
    def _attempt_copy(
        current: LastTurnAttempt,
        *,
        status: str,
        updated_at: datetime,
        error_category: str | None,
    ) -> LastTurnAttempt:
        return LastTurnAttempt.model_validate(
            current.model_dump(mode="python")
            | {
                "status": status,
                "updated_at": updated_at,
                "error_category": error_category,
            }
        )

    @staticmethod
    def _require_active_attempt(
        parent: ChatSessionRecord,
        command: TurnCommand,
        request_hash: str,
        run_id: UUID,
        expected_version: int,
    ) -> None:
        attempt = parent.last_attempt
        if (
            attempt is None
            or attempt.request_id != command.request_id
            or attempt.request_hash != request_hash
            or attempt.expected_version != expected_version
            or attempt.run_id != run_id
            or attempt.status != "processing"
        ):
            raise AppWriteConflict("Saved-turn attempt changed.")

    @staticmethod
    def _require_same_turn(turn: ChatTurnRecord, request_hash: str) -> None:
        if turn.request_hash != request_hash:
            raise AppWriteConflict("Turn request payload changed.")

    @staticmethod
    def _require_releasable_turn(
        decision: AuthorizationDecision,
        session_id: UUID,
        turn: ChatTurnRecord,
    ) -> None:
        if (
            turn.owner_user_id != decision.principal.user_id
            or turn.session_id != session_id
            or turn.policy_version != decision.policy_version
        ):
            raise AuthorizationDenied("Saved-turn access is not authorized.")

    async def _reconcile_unknown_commit(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        run_id: UUID,
        request_hash: str,
    ) -> TurnCommitResult:
        decision = await self._recheck_turn_decision(decision, session_id)
        now = self._now()
        user_id = decision.principal.user_id
        context = self._turn_context(user_id)
        async with self._database.saved_turn_transaction(context) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = await transaction.find_chat_session(session_id)
            self._require_live_owned_record(parent, user_id, now)
            committed = await transaction.find_chat_turn(session_id, command.request_id)
            if committed is not None:
                self._require_same_turn(committed, request_hash)
                self._require_releasable_turn(decision, session_id, committed)
                result = TurnCommitResult(
                    status="committed", turn=committed, replayed=True
                )
            else:
                current = parent.last_attempt
                if (
                    current is None
                    or current.run_id != run_id
                    or current.request_hash != request_hash
                    or current.status != "processing"
                ):
                    raise AppDatabaseUnavailable(
                        "Saved-turn outcome cannot be reconciled."
                    )
                unknown = self._attempt_copy(
                    current,
                    status="unknown",
                    updated_at=now,
                    error_category=None,
                )
                await transaction.finish_chat_attempt(
                    session_id, unknown, previous_status="processing"
                )
                result = TurnCommitResult(status="unknown")
        return result

    async def _recover_attempt_race(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        request_hash: str,
    ) -> TurnAttemptResult | None:
        """Read the winner of a same-chat attempt race in a fresh transaction."""
        decision = await self._recheck_turn_decision(decision, session_id)
        now = self._now()
        user_id = decision.principal.user_id
        async with self._database.saved_turn_transaction(
            self._turn_context(user_id)
        ) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = await transaction.find_chat_session(session_id)
            self._require_live_owned_record(parent, user_id, now)
            committed = await transaction.find_chat_turn(session_id, command.request_id)
            if committed is not None:
                self._require_same_turn(committed, request_hash)
                self._require_releasable_turn(decision, session_id, committed)
                result = TurnAttemptResult(status="committed", turn=committed)
            else:
                current = parent.last_attempt
                if (
                    parent.version == command.expected_version
                    and current is not None
                    and current.request_id == command.request_id
                    and current.request_hash == request_hash
                    and current.status == "processing"
                ):
                    result = TurnAttemptResult(
                        status="processing", run_id=current.run_id
                    )
                else:
                    return None
        if result.status == "committed":
            await self._recheck_turn_decision(decision, session_id)
        return result

    async def _recover_committed_turn(
        self,
        decision: AuthorizationDecision,
        session_id: UUID,
        command: TurnCommand,
        request_hash: str,
    ) -> TurnCommitResult | None:
        """Return a winner from an identical concurrent commit, if present."""
        decision = await self._recheck_turn_decision(decision, session_id)
        now = self._now()
        user_id = decision.principal.user_id
        async with self._database.saved_turn_transaction(
            self._turn_context(user_id)
        ) as transaction:
            await self._require_current_turn_access(transaction, decision, now)
            parent = await transaction.find_chat_session(session_id)
            self._require_live_owned_record(parent, user_id, now)
            turn = await transaction.find_chat_turn(session_id, command.request_id)
            if turn is None:
                return None
            self._require_same_turn(turn, request_hash)
            self._require_releasable_turn(decision, session_id, turn)
            result = TurnCommitResult(status="committed", turn=turn, replayed=True)
        await self._recheck_turn_decision(decision, session_id)
        return result

    async def _require_current_review_access(
        self, transaction, decision: AuthorizationDecision, now: datetime
    ) -> None:
        await transaction.require_saved_review_access(
            decision.policy_version,
            login_id=decision.principal.login_id,
            login_generation=decision.principal.generation,
            now=now,
        )

    async def _recheck_saved_history_decision(
        self,
        decision: AuthorizationDecision,
        *,
        action: str,
        owner_user_id: UUID,
    ) -> None:
        if action == "review_saved_chats":
            self._require_review_decision(decision, owner_user_id=owner_user_id)
        else:
            self._require_decision(decision, action=action)
            if owner_user_id != decision.principal.user_id:
                raise AuthorizationDenied("Saved-chat access is not authorized.")
        if self._authorization_recheck is None:
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        try:
            current = await self._authorization_recheck(decision)
        except Exception:
            raise AuthorizationDenied("Saved-chat access is not authorized.") from None
        if (
            not isinstance(current, AuthorizationDecision)
            or current.principal != decision.principal
            or current.request != decision.request
            or current.policy_version != decision.policy_version
            or current.snapshot != decision.snapshot
            or current.constraints != decision.constraints
            or current.requires_review_audit != decision.requires_review_audit
        ):
            raise AuthorizationDenied("Saved-chat access is not authorized.")

    @staticmethod
    def _require_history_decision(decision: AuthorizationDecision) -> None:
        ChatRepository._require_decision(decision, action="history")

    @staticmethod
    def _require_review_decision(
        decision: AuthorizationDecision, *, owner_user_id: UUID | None = None
    ) -> None:
        if not isinstance(decision, AuthorizationDecision):
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        principal = decision.principal
        request = decision.request
        snapshot = decision.snapshot
        target_owner = request.owner_id
        if (
            request.action != "review_saved_chats"
            or request.mode != "saved"
            or type(target_owner) is not UUID
            or owner_user_id is not None
            and target_owner != owner_user_id
            or snapshot.user.id != principal.user_id
            or snapshot.user.approval_status != "approved"
            or snapshot.user.role_id != snapshot.role.id
            or not snapshot.role.active
            or "review_saved_chats" not in snapshot.role.capabilities
            or snapshot.login.id != principal.login_id
            or snapshot.login.user_id != principal.user_id
            or snapshot.login.generation != principal.generation
            or decision.requires_review_audit is not True
        ):
            raise AuthorizationDenied("Saved-chat review is not authorized.")

    @staticmethod
    def _require_live_review_record(
        record: ChatSessionRecord | None, owner_user_id: UUID, now: datetime
    ) -> ChatSessionRecord:
        if (
            not isinstance(record, ChatSessionRecord)
            or record.owner_user_id != owner_user_id
            or record.status != "active"
            or (record.expires_at is not None and record.expires_at <= now)
        ):
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        return record

    @staticmethod
    def _review_audit_event(
        actor_user_id: UUID, target_id: UUID, now: datetime
    ) -> AppAuditEvent:
        return AppAuditEvent(
            actor_id=actor_user_id,
            target_id=target_id,
            action="admin_review",
            outcome="success",
            request_id=uuid4(),
            created_at=now,
        )

    @staticmethod
    def _history_context(user_id: UUID) -> InternalAuthorizationContext:
        return InternalAuthorizationContext(
            actor_id=user_id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_IDENTITIES,
                    StorageCapability.READ_LOGINS,
                    StorageCapability.READ_POLICY,
                    StorageCapability.READ_CHATS,
                }
            ),
        )

    @staticmethod
    def _review_context(user_id: UUID) -> InternalAuthorizationContext:
        return InternalAuthorizationContext(
            actor_id=user_id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_IDENTITIES,
                    StorageCapability.READ_LOGINS,
                    StorageCapability.READ_POLICY,
                    StorageCapability.READ_CHATS,
                    StorageCapability.ADVANCE_ACCESS_CONTROL,
                    StorageCapability.WRITE_REQUIRED_AUDIT,
                }
            ),
        )

    @staticmethod
    def _deletion_context(user_id: UUID) -> InternalAuthorizationContext:
        return InternalAuthorizationContext(
            actor_id=user_id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_IDENTITIES,
                    StorageCapability.READ_LOGINS,
                    StorageCapability.READ_POLICY,
                    StorageCapability.READ_CHATS,
                    StorageCapability.WRITE_CHATS,
                }
            ),
        )

    @staticmethod
    def _require_decision(decision: AuthorizationDecision, *, action: str) -> None:
        if not isinstance(decision, AuthorizationDecision):
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        principal = decision.principal
        request = decision.request
        snapshot = decision.snapshot
        if (
            request.action != action
            or request.owner_id != principal.user_id
            or request.mode != "saved"
            or snapshot.user.id != principal.user_id
            or snapshot.user.approval_status != "approved"
            or snapshot.user.role_id != snapshot.role.id
            or not snapshot.role.active
            or "chat" not in snapshot.role.capabilities
        ):
            raise AuthorizationDenied("Saved-chat access is not authorized.")

    async def _recover_creation(
        self,
        decision: AuthorizationDecision,
        request_id: UUID,
        user_id: UUID,
    ) -> ChatSessionRecord | None:
        context = self._context(user_id, write=False)
        async with self._database.transaction(context) as transaction:
            await transaction.require_chat_access(decision.policy_version)
            current = await transaction.find_chat_session_by_request(request_id)
        if current is None:
            return None
        return self._require_live_owned_record(current, user_id, self._now())

    @staticmethod
    def _require_live_owned_record(
        record: ChatSessionRecord | None, user_id: UUID, now: datetime
    ) -> ChatSessionRecord:
        if (
            not isinstance(record, ChatSessionRecord)
            or record.owner_user_id != user_id
            or record.status != "active"
            or (record.expires_at is not None and record.expires_at <= now)
        ):
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        return record

    @staticmethod
    def _context(user_id: UUID, *, write: bool) -> InternalAuthorizationContext:
        capabilities = {
            StorageCapability.READ_IDENTITIES,
            StorageCapability.READ_POLICY,
            StorageCapability.READ_CHATS,
        }
        if write:
            capabilities.add(StorageCapability.WRITE_CHATS)
        return InternalAuthorizationContext(
            actor_id=user_id,
            capabilities=frozenset(capabilities),
        )

    def _now(self) -> datetime:
        try:
            return utc_datetime(self._clock())
        except Exception:
            raise AppDatabaseUnavailable("Saved-chat time is unavailable.") from None
