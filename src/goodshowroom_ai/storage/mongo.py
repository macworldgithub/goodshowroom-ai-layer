"""Fixed-target Mongo lifecycle and app-only write primitives."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, AsyncContextManager
from uuid import UUID

from bson.codec_options import CodecOptions
from pydantic import SecretStr
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.collation import Collation
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from goodshowroom_ai.storage.access_requests import AccessRequestRecord, scope_within
from goodshowroom_ai.storage.chat_sessions import (
    ChatQuestionState,
    ChatReviewPosition,
    ChatSessionPosition,
    ChatSessionRecord,
    LastTurnAttempt,
)
from goodshowroom_ai.storage.identities import RoleRecord, UserRecord
from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.settings import AppSetting, IdentityMapping

from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    MAX_APP_DOCUMENT_BYTES,
    InternalAuthorizationContext,
    StorageCapability,
    utc_datetime,
    validate_bson_document,
)

if TYPE_CHECKING:
    from goodshowroom_ai.storage.chat_turns import ChatTurnRecord

APP_DATABASE_NAME = "byd_ai_layer"
_ACCESS_CONTROL_SETTINGS_ID = "access_control"
_AUDIT_INDEXES = (
    (
        (("actor_id", ASCENDING), ("created_at", DESCENDING)),
        {"name": "ix_actor_created"},
    ),
    (
        (("target_id", ASCENDING), ("created_at", DESCENDING)),
        {"name": "ix_target_created"},
    ),
)
_CHAT_INDEXES = (
    (
        (("owner_user_id", ASCENDING), ("creation_request_id", ASCENDING)),
        {"unique": True, "name": "uq_owner_creation_request"},
    ),
    (
        (
            ("owner_user_id", ASCENDING),
            ("status", ASCENDING),
            ("last_activity_at", DESCENDING),
            ("_id", DESCENDING),
        ),
        {"name": "ix_owner_status_activity"},
    ),
    (
        (("last_activity_at", DESCENDING), ("_id", DESCENDING)),
        {"name": "ix_activity_review"},
    ),
)
_TURN_INDEXES = (
    (
        (("session_id", ASCENDING), ("request_id", ASCENDING)),
        {"unique": True, "name": "uq_session_request"},
    ),
    (
        (("session_id", ASCENDING), ("sequence", ASCENDING)),
        {"unique": True, "name": "uq_session_sequence"},
    ),
)


class AppDatabaseUnavailable(RuntimeError):
    """The app database is not configured or cannot be reached."""


class AppCommitOutcomeUnknown(AppDatabaseUnavailable):
    """MongoDB did not confirm whether a saved-turn commit completed."""


class SavedChatCleanupPending(AppDatabaseUnavailable):
    """A saved chat is safely blocked but still needs cleanup retries."""


class AuthorizationDenied(PermissionError):
    """A typed app write context did not authorize the requested operation."""


class AppWriteConflict(RuntimeError):
    """The access-control policy version did not match the expected version."""


class RequiredAuditMissing(RuntimeError):
    """A protected access-control write did not add its required audit event."""


ClientFactory = Callable[..., Any]


def _has_error_label(error: BaseException, label: str) -> bool:
    checker = getattr(error, "has_error_label", None)
    try:
        return callable(checker) and checker(label) is True
    except Exception:
        return False


def _json_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported stored value")


def _turn_attempt_document(attempt: LastTurnAttempt) -> dict[str, Any]:
    value = attempt.model_dump(mode="json")
    value["started_at"] = attempt.started_at
    value["updated_at"] = attempt.updated_at
    return value


def _question_state_document(
    state: ChatQuestionState | None,
) -> dict[str, Any] | None:
    if state is None:
        return None
    value = state.model_dump(mode="json")
    value["start_utc"] = state.start_utc
    value["end_utc"] = state.end_utc
    return value


class AppDatabase:
    """Private async client bound to the one approved application database."""

    __slots__ = ("__client", "__database", "__closed")

    def __init__(self, client: Any, database: Any) -> None:
        self.__client = client
        self.__database = database
        self.__closed = False

    @classmethod
    async def connect(
        cls,
        uri: SecretStr | str | None,
        *,
        client_factory: ClientFactory | None = None,
    ) -> "AppDatabase":
        """Connect only when called explicitly, and select a fixed app database."""
        if isinstance(uri, SecretStr):
            uri_value = uri.get_secret_value()
        elif isinstance(uri, str):
            uri_value = uri
        else:
            uri_value = ""
        if not uri_value.strip() or uri_value != uri_value.strip():
            raise AppDatabaseUnavailable("App database is not configured.")
        factory = client_factory or AsyncMongoClient
        try:
            client = factory(uri_value, tz_aware=True, serverSelectionTimeoutMS=5000)
            database = client[APP_DATABASE_NAME].with_options(
                codec_options=CodecOptions(tz_aware=True, tzinfo=UTC)
            )
            await client.admin.command("ping")
        except Exception:
            try:
                if "client" in locals():
                    await client.close()
            except Exception:
                pass
            raise AppDatabaseUnavailable("App database is unavailable.") from None
        return cls(client, database)

    async def close(self) -> None:
        """Close the private client once and discard its references."""
        if self.__closed:
            return
        self.__closed = True
        client = self.__client
        self.__client = None
        self.__database = None
        try:
            await client.close()
        except Exception:
            raise AppDatabaseUnavailable(
                "App database could not close cleanly."
            ) from None

    async def load_authorization_policy(self, principal):
        """Read current app policy in one snapshot, with no mutations or cache.

        Call only with a principal from the verified internal assertion boundary.
        Unsupported transactions or missing/malformed records deny in the service.
        """
        from goodshowroom_ai.authorization import Principal, PolicySnapshot

        if not isinstance(principal, Principal):
            raise AuthorizationDenied("Policy read is not authorized.")
        database = self.__require_database()
        context = InternalAuthorizationContext(
            actor_id=principal.user_id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_IDENTITIES,
                    StorageCapability.READ_LOGINS,
                    StorageCapability.READ_SETTINGS,
                    StorageCapability.READ_POLICY,
                }
            ),
        )
        reader = None
        try:
            async with self.__client.start_session().bind() as session:
                async with await session.start_transaction(
                    read_concern=ReadConcern("snapshot")
                ):
                    reader = _AppWriteTransaction(database, session, context)
                    version = await reader.get_policy_version()
                    user = await reader.get_user(principal.user_id)
                    if user is None or user.role_id is None:
                        raise AuthorizationDenied("Policy read is not authorized.")
                    role = await reader.get_role(user.role_id)
                    login = await reader.get_login(principal.login_id)
                    if role is None or login is None:
                        raise AuthorizationDenied("Policy read is not authorized.")
                    result = PolicySnapshot(
                        policy_version=version,
                        user=user,
                        role=role,
                        login=login,
                        mappings=await reader.list_active_mappings(principal.user_id),
                        sites=await reader.get_setting("sites"),
                    )
                    reader._verify_can_commit()
                return result
        except AuthorizationDenied:
            raise
        except Exception:
            raise AppDatabaseUnavailable("Policy is unavailable.") from None
        finally:
            if reader is not None:
                reader._deactivate()

    async def initialize_indexes(self) -> None:
        """Create the TICKET-021 audit indexes; never drop or repair an index."""
        database = self.__require_database()
        try:
            collection = database["audit_events"]
            for keys, options in _AUDIT_INDEXES:
                await collection.create_index(list(keys), **options)
        except Exception:
            raise AppDatabaseUnavailable(
                "App database indexes are unavailable."
            ) from None

    def transaction(
        self, context: InternalAuthorizationContext
    ) -> AsyncContextManager["_AppWriteTransaction"]:
        """Open a typed transaction; no raw client, DB, or collection is exposed."""
        if (
            not isinstance(context, InternalAuthorizationContext)
            or context.actor_id is None
            or not context.capabilities
        ):
            raise AuthorizationDenied("App write is not authorized.")
        return self.__write_transaction(context)

    def saved_turn_transaction(
        self, context: InternalAuthorizationContext
    ) -> AsyncContextManager["_AppWriteTransaction"]:
        """Open a short majority-committed transaction for one saved turn."""
        if (
            not isinstance(context, InternalAuthorizationContext)
            or context.actor_id is None
            or not context.capabilities
            or StorageCapability.WRITE_CHATS not in context.capabilities
        ):
            raise AuthorizationDenied("Saved-turn write is not authorized.")
        return self.__write_transaction(context, saved_turn=True)

    def snapshot_transaction(
        self, context: InternalAuthorizationContext
    ) -> AsyncContextManager["_AppWriteTransaction"]:
        """Open a bounded snapshot transaction for protected history reads."""
        if (
            not isinstance(context, InternalAuthorizationContext)
            or context.actor_id is None
            or not context.capabilities
        ):
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        return self.__write_transaction(context, saved_turn=True)

    async def initialize_identity_indexes(self) -> None:
        """Explicit app-only indexes. No automatic migration or index repair."""
        database = self.__require_database()
        try:
            await database["users"].create_index(
                [("provider", 1), ("provider_account_id", 1)],
                unique=True,
                name="uq_provider_account",
            )
            await database["users"].create_index(
                [("email_key", 1)], unique=True, name="uq_email_key"
            )
            await database["users"].create_index(
                [("approval_status", 1), ("updated_at", 1)], name="ix_approval_updated"
            )
            await database["roles"].create_index(
                [("name_key", 1)], unique=True, name="uq_name_key"
            )
        except Exception:
            raise AppDatabaseUnavailable("Identity indexes are unavailable.") from None

    async def initialize_access_request_indexes(self) -> None:
        """Explicit app-only partial uniqueness; never drop existing indexes."""
        try:
            collection = self.__require_database()["access_requests"]
            await collection.create_index(
                [("kind", 1), ("email_key", 1)],
                unique=True,
                partialFilterExpression={"active": True},
                name="uq_active_kind_email",
            )
            await collection.create_index(
                [("status", 1), ("created_at", 1)], name="ix_status_created"
            )
        except Exception:
            raise AppDatabaseUnavailable(
                "Access request indexes are unavailable."
            ) from None

    async def initialize_login_indexes(self) -> None:
        try:
            collection = self.__require_database()["login_sessions"]
            await collection.create_index(
                [("user_id", 1), ("revoked_at", 1)], name="ix_user_revoked"
            )
            await collection.create_index(
                [("purge_after", 1)], expireAfterSeconds=0, name="ttl_login_purge"
            )
        except Exception:
            raise AppDatabaseUnavailable("Login indexes are unavailable.") from None

    async def initialize_mapping_indexes(self) -> None:
        try:
            collection = self.__require_database()["identity_mappings"]
            await collection.create_index(
                [
                    ("source_ref.database", 1),
                    ("source_ref.collection", 1),
                    ("source_ref.key_field", 1),
                    ("source_ref.key_type", 1),
                    ("source_ref.key_value", 1),
                ],
                unique=True,
                name="uq_source_identity",
            )
            await collection.create_index(
                [("user_id", 1), ("active", 1)], name="ix_user_active"
            )
        except Exception:
            raise AppDatabaseUnavailable("Mapping indexes are unavailable.") from None

    async def initialize_chat_indexes(self) -> None:
        """Create only the approved app-only saved-chat indexes."""
        try:
            collection = self.__require_database()["chat_sessions"]
            for keys, options in _CHAT_INDEXES:
                await collection.create_index(list(keys), **options)
        except Exception:
            raise AppDatabaseUnavailable(
                "Saved-chat indexes are unavailable."
            ) from None

    async def initialize_turn_indexes(self) -> None:
        """Create only the approved unique app-only turn indexes."""
        try:
            collection = self.__require_database()["chat_turns"]
            for keys, options in _TURN_INDEXES:
                await collection.create_index(list(keys), **options)
        except Exception:
            raise AppDatabaseUnavailable(
                "Saved-turn indexes are unavailable."
            ) from None

    @asynccontextmanager
    async def __write_transaction(
        self,
        context: InternalAuthorizationContext,
        *,
        saved_turn: bool = False,
    ) -> AsyncIterator["_AppWriteTransaction"]:
        database = self.__require_database()
        transaction = None
        try:
            async with self.__client.start_session().bind() as session:
                transaction_options = (
                    {
                        "read_concern": ReadConcern("snapshot"),
                        "write_concern": WriteConcern("majority"),
                        "max_commit_time_ms": 5_000,
                    }
                    if saved_turn
                    else {}
                )
                async with await session.start_transaction(**transaction_options):
                    transaction = _AppWriteTransaction(database, session, context)
                    yield transaction
                    transaction._verify_can_commit()
        except (
            AuthorizationDenied,
            AppWriteConflict,
            RequiredAuditMissing,
            ValueError,
        ):
            raise
        except AppDatabaseUnavailable:
            raise
        except Exception as error:
            if _has_error_label(error, "UnknownTransactionCommitResult"):
                raise AppCommitOutcomeUnknown(
                    "Saved-turn commit outcome is unknown."
                ) from None
            if _has_error_label(error, "TransientTransactionError"):
                raise AppWriteConflict("App transaction conflicted.") from None
            raise AppDatabaseUnavailable("App transaction failed.") from None
        finally:
            if transaction is not None:
                transaction._deactivate()

    def __require_database(self) -> Any:
        if self.__closed or self.__database is None or self.__client is None:
            raise AppDatabaseUnavailable("App database is unavailable.")
        return self.__database


class _AppWriteTransaction:
    __slots__ = (
        "__database",
        "__authorization",
        "__session",
        "__fence_advanced",
        "__audit_written",
        "__poisoned",
        "__active",
        "__identity_changed",
        "__review_authorized",
    )

    def __init__(
        self, database: Any, session: Any, authorization: InternalAuthorizationContext
    ):
        self.__database = database
        self.__authorization = authorization
        self.__session = session
        self.__fence_advanced = False
        self.__audit_written = False
        self.__poisoned = False
        self.__active = True
        self.__identity_changed = False
        self.__review_authorized = False

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        self.__require(StorageCapability.READ_IDENTITIES)
        return await self.__read_identity("users", user_id, UserRecord)

    async def get_role(self, role_id: UUID) -> RoleRecord | None:
        self.__require(StorageCapability.READ_IDENTITIES)
        return await self.__read_identity("roles", role_id, RoleRecord)

    async def get_policy_version(self) -> int:
        self.__require(StorageCapability.READ_POLICY)
        try:
            value = await self.__database["app_settings"].find_one(
                {"_id": _ACCESS_CONTROL_SETTINGS_ID},
                session=self.__session,
            )
            version = value.get("policy_version") if value is not None else None
            if type(version) is not int or version < 0:
                raise ValueError()
            return version
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Policy is unavailable.") from None

    async def require_chat_access(self, expected_policy_version: int) -> None:
        """Recheck owner approval, active chat role and policy in this transaction."""
        self.__require(StorageCapability.READ_CHATS)
        if type(expected_policy_version) is not int or expected_policy_version < 0:
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        try:
            if await self.get_policy_version() != expected_policy_version:
                raise AppWriteConflict("Saved-chat policy changed.")
            user = await self.get_user(self.__authorization.actor_id)
            if (
                user is None
                or user.approval_status != "approved"
                or user.role_id is None
                or not user.scope_limit
            ):
                raise AuthorizationDenied("Saved-chat access is not authorized.")
            role = await self.get_role(user.role_id)
            if (
                role is None
                or not role.active
                or "chat" not in role.capabilities
                or any(
                    not any(scope_within(scope, grant) for grant in role.grants)
                    for scope in user.scope_limit
                )
            ):
                raise AuthorizationDenied("Saved-chat access is not authorized.")
        except (AuthorizationDenied, AppWriteConflict, AppDatabaseUnavailable):
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat access is unavailable.") from None

    async def require_saved_turn_access(
        self,
        expected_policy_version: int,
        *,
        login_id: UUID,
        login_generation: int,
        now: datetime,
    ) -> None:
        """Require the current app policy and an active login for a saved turn."""
        self.__require(StorageCapability.READ_LOGINS)
        await self.require_chat_access(expected_policy_version)
        now = utc_datetime(now)
        principal = self.__authorization.actor_id
        if type(login_id) is not UUID:
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        login = await self.get_login(login_id)
        if (
            login is None
            or login.user_id != principal
            or login.generation != login_generation
            or not login.valid(now)
        ):
            raise AuthorizationDenied("Saved-chat access is not authorized.")

    async def require_saved_review_access(
        self,
        expected_policy_version: int,
        *,
        login_id: UUID,
        login_generation: int,
        now: datetime,
    ) -> None:
        """Require the reviewer identity and explicit review capability."""
        self.__require(StorageCapability.READ_CHATS)
        self.__require(StorageCapability.READ_IDENTITIES)
        self.__require(StorageCapability.READ_LOGINS)
        now = utc_datetime(now)
        actor_id = self.__authorization.actor_id
        if type(login_id) is not UUID or await self.get_policy_version() != (
            expected_policy_version
        ):
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        user = await self.get_user(actor_id)
        if (
            user is None
            or user.approval_status != "approved"
            or user.role_id is None
            or not user.scope_limit
        ):
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        role = await self.get_role(user.role_id)
        if (
            role is None
            or not role.active
            or "review_saved_chats" not in role.capabilities
            or any(
                not any(scope_within(scope, grant) for grant in role.grants)
                for scope in user.scope_limit
            )
        ):
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        login = await self.get_login(login_id)
        if (
            login is None
            or login.user_id != actor_id
            or login.generation != login_generation
            or not login.valid(now)
        ):
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        self.__review_authorized = True

    async def find_chat_session_by_request(
        self, request_id: UUID
    ) -> ChatSessionRecord | None:
        """Find an idempotency record within the authenticated owner's scope."""
        self.__require(StorageCapability.READ_CHATS)
        if type(request_id) is not UUID:
            raise ValueError("Saved-chat request ID must be a UUID")
        try:
            document = await self.__database["chat_sessions"].find_one(
                {
                    "owner_user_id": str(self.__authorization.actor_id),
                    "creation_request_id": str(request_id),
                },
                session=self.__session,
            )
            return self.__chat_session_from_document(document)
        except AppDatabaseUnavailable:
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat record is unavailable.") from None

    async def find_chat_session(self, session_id: UUID) -> ChatSessionRecord | None:
        self.__require(StorageCapability.READ_CHATS)
        if type(session_id) is not UUID:
            raise AuthorizationDenied("Saved-chat access is not authorized.")
        try:
            document = await self.__database["chat_sessions"].find_one(
                {
                    "_id": str(session_id),
                    "owner_user_id": str(self.__authorization.actor_id),
                },
                session=self.__session,
            )
            return self.__chat_session_from_document(document)
        except (AuthorizationDenied, AppDatabaseUnavailable):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat record is unavailable.") from None

    async def find_review_chat_session(
        self, owner_user_id: UUID, session_id: UUID, *, now: datetime
    ) -> ChatSessionRecord | None:
        """Find one live target chat only after current reviewer checks."""
        self.__require_review_access()
        if type(owner_user_id) is not UUID or type(session_id) is not UUID:
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        try:
            document = await self.__database["chat_sessions"].find_one(
                {
                    "_id": str(session_id),
                    "owner_user_id": str(owner_user_id),
                    "status": "active",
                    "$or": [
                        {"expires_at": {"$exists": False}},
                        {"expires_at": None},
                        {"expires_at": {"$gt": utc_datetime(now)}},
                    ],
                },
                max_time_ms=5_000,
                session=self.__session,
            )
            record = self.__chat_session_from_document(document)
            if record is not None and (
                record.owner_user_id != owner_user_id
                or record.status != "active"
                or record.expires_at is not None
                and record.expires_at <= utc_datetime(now)
            ):
                raise ValueError("Review query returned an invalid chat")
            return record
        except (AuthorizationDenied, AppDatabaseUnavailable):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat review is unavailable.") from None

    async def find_chat_turn(
        self, session_id: UUID, request_id: UUID
    ) -> ChatTurnRecord | None:
        self.__require(StorageCapability.READ_CHATS)
        if type(session_id) is not UUID or type(request_id) is not UUID:
            raise AuthorizationDenied("Saved-turn access is not authorized.")
        try:
            document = await self.__database["chat_turns"].find_one(
                {
                    "session_id": str(session_id),
                    "owner_user_id": str(self.__authorization.actor_id),
                    "request_id": str(request_id),
                },
                session=self.__session,
            )
            turn = self.__chat_turn_from_document(document)
            if turn is not None and (
                turn.session_id != session_id
                or turn.owner_user_id != self.__authorization.actor_id
                or turn.request_id != request_id
            ):
                raise ValueError("Saved-turn query returned a mismatched record")
            return turn
        except (AuthorizationDenied, AppDatabaseUnavailable):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-turn record is unavailable.") from None

    async def list_chat_turns(
        self,
        session_id: UUID,
        *,
        snapshot_version: int,
        after_sequence: int,
        page_size: int,
    ) -> tuple[tuple[ChatTurnRecord, ...], bool]:
        """Read one bounded, ascending page for the authenticated owner."""
        self.__require(StorageCapability.READ_CHATS)
        return await self.__list_chat_turns(
            self.__authorization.actor_id,
            session_id,
            snapshot_version=snapshot_version,
            after_sequence=after_sequence,
            page_size=page_size,
        )

    async def list_review_chat_turns(
        self,
        owner_user_id: UUID,
        session_id: UUID,
        *,
        snapshot_version: int,
        after_sequence: int,
        page_size: int,
    ) -> tuple[tuple[ChatTurnRecord, ...], bool]:
        """Read a target owner's bounded turn page after reviewer checks."""
        self.__require_review_access()
        if type(owner_user_id) is not UUID:
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        return await self.__list_chat_turns(
            owner_user_id,
            session_id,
            snapshot_version=snapshot_version,
            after_sequence=after_sequence,
            page_size=page_size,
        )

    async def __list_chat_turns(
        self,
        owner_user_id: UUID,
        session_id: UUID,
        *,
        snapshot_version: int,
        after_sequence: int,
        page_size: int,
    ) -> tuple[tuple[ChatTurnRecord, ...], bool]:
        if (
            type(owner_user_id) is not UUID
            or type(session_id) is not UUID
            or type(snapshot_version) is not int
            or snapshot_version < 0
            or type(after_sequence) is not int
            or after_sequence < 0
            or type(page_size) is not int
            or not 1 <= page_size <= 100
        ):
            raise AuthorizationDenied("Saved-turn page is not authorized.")
        if after_sequence > snapshot_version:
            raise AuthorizationDenied("Saved-turn page is not authorized.")
        query = {
            "session_id": str(session_id),
            "owner_user_id": str(owner_user_id),
            "sequence": {"$gt": after_sequence, "$lte": snapshot_version},
        }
        projection = {
            "_id": 1,
            "session_id": 1,
            "owner_user_id": 1,
            "request_id": 1,
            "request_hash": 1,
            "expected_version": 1,
            "sequence": 1,
            "user_message": 1,
            "response": 1,
            "question_state_after": 1,
            "policy_version": 1,
            "metric_versions": 1,
            "committed_at": 1,
            "expires_at": 1,
            "size_bytes": 1,
        }
        cursor = None
        try:
            cursor = self.__database["chat_turns"].find(
                query,
                projection=projection,
                max_time_ms=5_000,
                session=self.__session,
            )
            documents = await (
                cursor.sort([("sequence", ASCENDING)])
                .limit(page_size + 1)
                .batch_size(1)
                .to_list(length=page_size + 1)
            )
            has_more = len(documents) > page_size
            rows = tuple(
                self.__chat_turn_from_document(document)
                for document in documents[:page_size]
            )
            if any(
                row is None
                or row.session_id != session_id
                or row.owner_user_id != owner_user_id
                or not after_sequence < row.sequence <= snapshot_version
                for row in rows
            ):
                raise ValueError("Saved-turn page returned an invalid record")
            if tuple(row.sequence for row in rows) != tuple(
                sorted(row.sequence for row in rows)
            ):
                raise ValueError("Saved-turn page order is invalid")
            return rows, has_more
        except (AuthorizationDenied, AppDatabaseUnavailable):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-turn page is unavailable.") from None
        finally:
            if cursor is not None:
                try:
                    await cursor.close()
                except Exception:
                    self.__poisoned = True
                    raise AppDatabaseUnavailable(
                        "Saved-turn page is unavailable."
                    ) from None

    async def begin_chat_attempt(
        self,
        session_id: UUID,
        expected_version: int,
        expected_attempt: LastTurnAttempt | None,
        attempt: LastTurnAttempt,
    ) -> None:
        """Persist only bounded attempt metadata before model/source execution."""
        self.__require(StorageCapability.WRITE_CHATS)
        if (
            type(session_id) is not UUID
            or type(expected_version) is not int
            or expected_version < 0
            or (
                expected_attempt is not None
                and not isinstance(expected_attempt, LastTurnAttempt)
            )
            or not isinstance(attempt, LastTurnAttempt)
            or attempt.status != "processing"
        ):
            raise ValueError("Saved-turn attempt is invalid")
        try:
            query = {
                "_id": str(session_id),
                "owner_user_id": str(self.__authorization.actor_id),
                "status": "active",
                "version": expected_version,
            }
            if expected_attempt is None:
                query["last_attempt"] = None
            else:
                query["last_attempt.run_id"] = str(expected_attempt.run_id)
                query["last_attempt.request_hash"] = expected_attempt.request_hash
                query["last_attempt.status"] = expected_attempt.status
            result = await self.__database["chat_sessions"].update_one(
                query,
                {"$set": {"last_attempt": _turn_attempt_document(attempt)}},
                session=self.__session,
            )
            if result.matched_count != 1:
                self.__poisoned = True
                raise AppWriteConflict("Saved-chat version changed.")
        except (AppWriteConflict, AuthorizationDenied):
            raise
        except Exception as error:
            if (
                _has_error_label(error, "TransientTransactionError")
                or getattr(error, "code", None) == 112
            ):
                self.__poisoned = True
                raise AppWriteConflict("Saved-turn attempt changed.") from None
            self.__poisoned = True
            raise AppDatabaseUnavailable(
                "Saved-turn attempt could not be saved."
            ) from None

    async def finish_chat_attempt(
        self,
        session_id: UUID,
        attempt: LastTurnAttempt,
        *,
        previous_status: str = "processing",
    ) -> None:
        self.__require(StorageCapability.WRITE_CHATS)
        if (
            type(session_id) is not UUID
            or not isinstance(attempt, LastTurnAttempt)
            or attempt.status not in {"failed", "interrupted", "unknown"}
        ):
            raise ValueError("Saved-turn attempt update is invalid")
        try:
            result = await self.__database["chat_sessions"].update_one(
                {
                    "_id": str(session_id),
                    "owner_user_id": str(self.__authorization.actor_id),
                    "last_attempt.request_id": str(attempt.request_id),
                    "last_attempt.request_hash": attempt.request_hash,
                    "last_attempt.run_id": str(attempt.run_id),
                    "last_attempt.status": previous_status,
                },
                {"$set": {"last_attempt": _turn_attempt_document(attempt)}},
                session=self.__session,
            )
            if result.matched_count != 1:
                self.__poisoned = True
                raise AppWriteConflict("Saved-turn attempt changed.")
        except (AppWriteConflict, AuthorizationDenied):
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-turn attempt update failed.") from None

    async def commit_chat_turn(
        self,
        turn: ChatTurnRecord,
        attempt: LastTurnAttempt,
    ) -> None:
        """Insert one validated turn and advance its parent inside this txn."""
        from goodshowroom_ai.storage.chat_turns import ChatTurnRecord

        self.__require(StorageCapability.WRITE_CHATS)
        if (
            not isinstance(turn, ChatTurnRecord)
            or not isinstance(attempt, LastTurnAttempt)
            or attempt.status != "committed"
            or turn.owner_user_id != self.__authorization.actor_id
            or turn.request_id != attempt.request_id
            or turn.request_hash != attempt.request_hash
            or turn.sequence != attempt.expected_version + 1
        ):
            raise AuthorizationDenied("Saved-turn write is not authorized.")
        try:
            await self.__database["chat_turns"].insert_one(
                turn.document(), session=self.__session
            )
            parent_update = {
                "version": turn.sequence,
                "updated_at": turn.committed_at,
                "last_activity_at": turn.committed_at,
                "context_state": _question_state_document(turn.question_state_after),
                "summary": None,
                "last_attempt": _turn_attempt_document(attempt),
            }
            result = await self.__database["chat_sessions"].update_one(
                {
                    "_id": str(turn.session_id),
                    "owner_user_id": str(turn.owner_user_id),
                    "status": "active",
                    "version": turn.expected_version,
                    "expires_at": turn.expires_at,
                    "last_attempt.run_id": str(attempt.run_id),
                    "last_attempt.status": "processing",
                    "last_attempt.request_hash": turn.request_hash,
                },
                {"$set": parent_update},
                session=self.__session,
            )
            if result.matched_count != 1:
                self.__poisoned = True
                raise AppWriteConflict("Saved-chat version or attempt changed.")
        except DuplicateKeyError:
            self.__poisoned = True
            raise AppWriteConflict(
                "Saved-turn request or sequence already exists."
            ) from None
        except (AppWriteConflict, AuthorizationDenied):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-turn commit failed.") from None

    async def record_saved_turn_audit(self, event: AppAuditEvent) -> None:
        """Append the fixed, content-free audit event required for turn commit."""
        await self.append_required_audit(event)

    async def record_saved_review_audit(
        self, event: AppAuditEvent, *, expected_policy_version: int
    ) -> None:
        """Fence and audit one authorized saved-chat review before release."""
        self.__require_review_access()
        if not isinstance(event, AppAuditEvent) or event.action != "admin_review":
            raise AuthorizationDenied("Saved-chat review is not authorized.")
        await self.advance_access_control_fence(expected_policy_version)
        await self.append_required_audit(event)

    async def mark_chat_deleting(
        self, session_id: UUID, *, now: datetime
    ) -> ChatSessionRecord | None:
        """Block new turns and clear all parent-stored conversation derivatives."""
        self.__require(StorageCapability.READ_CHATS)
        self.__require(StorageCapability.WRITE_CHATS)
        if type(session_id) is not UUID:
            raise AuthorizationDenied("Saved-chat deletion is not authorized.")
        parent = await self.find_chat_session(session_id)
        if parent is None or parent.status == "deleting":
            return parent
        try:
            result = await self.__database["chat_sessions"].update_one(
                {
                    "_id": str(session_id),
                    "owner_user_id": str(self.__authorization.actor_id),
                    "status": "active",
                    "version": parent.version,
                },
                {
                    "$set": {
                        "status": "deleting",
                        "updated_at": utc_datetime(now),
                        "context_state": None,
                        "summary": None,
                        "last_attempt": None,
                    },
                    "$inc": {"version": 1},
                },
                session=self.__session,
            )
            if result.matched_count != 1:
                self.__poisoned = True
                raise AppWriteConflict("Saved-chat deletion changed.")
            return parent.model_copy(
                update={
                    "status": "deleting",
                    "version": parent.version + 1,
                    "updated_at": utc_datetime(now),
                    "context_state": None,
                    "summary": None,
                    "last_attempt": None,
                }
            )
        except (AppWriteConflict, AuthorizationDenied):
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable(
                "Saved-chat deletion could not start."
            ) from None

    async def delete_chat_turns(self, session_id: UUID, *, batch_size: int) -> bool:
        """Remove one bounded turn batch after the owned parent is deleting."""
        self.__require(StorageCapability.READ_CHATS)
        self.__require(StorageCapability.WRITE_CHATS)
        if (
            type(session_id) is not UUID
            or type(batch_size) is not int
            or not 1 <= batch_size <= 100
        ):
            raise AuthorizationDenied("Saved-chat deletion is not authorized.")
        parent = await self.find_chat_session(session_id)
        if parent is None:
            return False
        if parent.status != "deleting":
            raise AuthorizationDenied("Saved-chat deletion is not authorized.")
        cursor = None
        try:
            cursor = self.__database["chat_turns"].find(
                {"session_id": str(session_id)},
                projection={"_id": 1},
                max_time_ms=5_000,
                session=self.__session,
            )
            documents = await (
                cursor.sort([("_id", ASCENDING)])
                .limit(batch_size + 1)
                .batch_size(1)
                .to_list(length=batch_size + 1)
            )
            has_more = len(documents) > batch_size
            identifiers = [document["_id"] for document in documents[:batch_size]]
            if identifiers:
                await self.__database["chat_turns"].delete_many(
                    {
                        "session_id": str(session_id),
                        "_id": {"$in": identifiers},
                    },
                    session=self.__session,
                )
            return has_more
        except AuthorizationDenied:
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat cleanup is incomplete.") from None
        finally:
            if cursor is not None:
                try:
                    await cursor.close()
                except Exception:
                    self.__poisoned = True
                    raise AppDatabaseUnavailable(
                        "Saved-chat cleanup is incomplete."
                    ) from None

    async def delete_chat_session(self, session_id: UUID) -> None:
        """Remove the deleting parent after all of its turns were removed."""
        self.__require(StorageCapability.READ_CHATS)
        self.__require(StorageCapability.WRITE_CHATS)
        if type(session_id) is not UUID:
            raise AuthorizationDenied("Saved-chat deletion is not authorized.")
        parent = await self.find_chat_session(session_id)
        if parent is None:
            return
        if parent.status != "deleting":
            raise AuthorizationDenied("Saved-chat deletion is not authorized.")
        try:
            result = await self.__database["chat_sessions"].delete_one(
                {
                    "_id": str(session_id),
                    "owner_user_id": str(self.__authorization.actor_id),
                    "status": "deleting",
                },
                session=self.__session,
            )
            if result.deleted_count != 1:
                current = await self.find_chat_session(session_id)
                if current is not None:
                    self.__poisoned = True
                    raise AppWriteConflict("Saved-chat cleanup is incomplete.")
        except AppWriteConflict:
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat cleanup is incomplete.") from None

    async def insert_chat_session(self, record: ChatSessionRecord) -> None:
        """Insert one initial saved-chat parent without changing access policy."""
        self.__require(StorageCapability.WRITE_CHATS)
        if (
            not isinstance(record, ChatSessionRecord)
            or record.owner_user_id != self.__authorization.actor_id
            or record.version != 0
            or record.status != "active"
            or record.title != "New chat"
        ):
            raise AuthorizationDenied("Saved-chat write is not authorized.")
        document = record.document()
        try:
            await self.__database["chat_sessions"].insert_one(
                document, session=self.__session
            )
        except DuplicateKeyError:
            self.__poisoned = True
            raise AppWriteConflict(
                "Saved-chat creation changed; retry the request."
            ) from None
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat creation failed.") from None

    async def list_chat_sessions(
        self,
        *,
        now: datetime,
        page_size: int,
        after: ChatSessionPosition | None = None,
    ) -> tuple[tuple[ChatSessionRecord, ...], bool]:
        """Read a bounded page of current owner's active saved-chat metadata."""
        self.__require(StorageCapability.READ_CHATS)
        if (
            type(page_size) is not int
            or not 1 <= page_size <= 100
            or (after is not None and not isinstance(after, ChatSessionPosition))
        ):
            raise ValueError("Saved-chat page request is invalid")
        now = utc_datetime(now)
        owner_id = self.__authorization.actor_id
        if after is not None and (
            after.owner_user_id != owner_id or after.page_size != page_size
        ):
            raise AuthorizationDenied("Saved-chat page is not authorized.")

        filters: list[dict[str, Any]] = [
            {
                "$or": [
                    {"expires_at": {"$exists": False}},
                    {"expires_at": None},
                    {"expires_at": {"$gt": now}},
                ]
            }
        ]
        if after is not None:
            if type(after.session_id) is not UUID:
                raise ValueError("Saved-chat cursor is invalid")
            filters.append(
                {
                    "$or": [
                        {"last_activity_at": {"$lt": after.last_activity_at}},
                        {
                            "last_activity_at": after.last_activity_at,
                            "_id": {"$lt": str(after.session_id)},
                        },
                    ]
                }
            )
        query: dict[str, Any] = {
            "owner_user_id": str(owner_id),
            "status": "active",
            "$and": filters,
        }
        projection = {
            "_id": 1,
            "schema_version": 1,
            "version": 1,
            "created_at": 1,
            "updated_at": 1,
            "owner_user_id": 1,
            "creation_request_id": 1,
            "title": 1,
            "status": 1,
            "last_activity_at": 1,
            "expires_at": 1,
        }
        cursor = None
        try:
            cursor = self.__database["chat_sessions"].find(
                query,
                projection=projection,
                collation=Collation(locale="simple"),
                max_time_ms=5_000,
                session=self.__session,
            )
            documents = await (
                cursor.sort([("last_activity_at", DESCENDING), ("_id", DESCENDING)])
                .limit(page_size + 1)
                .batch_size(1)
                .to_list(length=page_size + 1)
            )
            has_more = len(documents) > page_size
            rows = tuple(
                self.__chat_session_from_document(document)
                for document in documents[:page_size]
            )
            if any(
                row is None
                or row.owner_user_id != owner_id
                or row.status != "active"
                or (row.expires_at is not None and row.expires_at <= now)
                for row in rows
            ):
                raise ValueError("Saved-chat query returned an invalid record")
            return rows, has_more
        except (AuthorizationDenied, AppDatabaseUnavailable):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat list is unavailable.") from None
        finally:
            if cursor is not None:
                try:
                    await cursor.close()
                except Exception:
                    self.__poisoned = True
                    raise AppDatabaseUnavailable(
                        "Saved-chat list is unavailable."
                    ) from None

    async def list_review_chat_sessions(
        self,
        owner_user_id: UUID,
        *,
        now: datetime,
        page_size: int,
        since: datetime | None = None,
        until: datetime | None = None,
        after: ChatReviewPosition | None = None,
        policy_version: int,
    ) -> tuple[tuple[ChatSessionRecord, ...], bool]:
        """Read a bounded target-owner page after current reviewer checks."""
        self.__require_review_access()
        if (
            type(owner_user_id) is not UUID
            or type(page_size) is not int
            or not 1 <= page_size <= 100
            or type(policy_version) is not int
            or policy_version < 0
            or (since is None) != (until is None)
            or (after is not None and not isinstance(after, ChatReviewPosition))
        ):
            raise AuthorizationDenied("Saved-chat review page is not authorized.")
        now = utc_datetime(now)
        since = utc_datetime(since) if since is not None else None
        until = utc_datetime(until) if until is not None else None
        if since is not None and until is not None and since >= until:
            raise AuthorizationDenied("Saved-chat review page is not authorized.")
        actor_id = self.__authorization.actor_id
        if after is not None and (
            after.actor_user_id != actor_id
            or after.owner_user_id != owner_user_id
            or after.policy_version != policy_version
            or after.page_size != page_size
            or after.since != since
            or after.until != until
        ):
            raise AuthorizationDenied("Saved-chat review page is not authorized.")

        filters: list[dict[str, Any]] = [
            {
                "$or": [
                    {"expires_at": {"$exists": False}},
                    {"expires_at": None},
                    {"expires_at": {"$gt": now}},
                ]
            }
        ]
        if since is not None and until is not None:
            filters.extend(
                [
                    {"last_activity_at": {"$gte": since}},
                    {"last_activity_at": {"$lt": until}},
                ]
            )
        if after is not None:
            filters.append(
                {
                    "$or": [
                        {"last_activity_at": {"$lt": after.last_activity_at}},
                        {
                            "last_activity_at": after.last_activity_at,
                            "_id": {"$lt": str(after.session_id)},
                        },
                    ]
                }
            )
        query = {
            "owner_user_id": str(owner_user_id),
            "status": "active",
            "$and": filters,
        }
        projection = {
            "_id": 1,
            "schema_version": 1,
            "version": 1,
            "created_at": 1,
            "updated_at": 1,
            "owner_user_id": 1,
            "creation_request_id": 1,
            "title": 1,
            "status": 1,
            "last_activity_at": 1,
            "expires_at": 1,
        }
        cursor = None
        try:
            cursor = self.__database["chat_sessions"].find(
                query,
                projection=projection,
                collation=Collation(locale="simple"),
                max_time_ms=5_000,
                session=self.__session,
            )
            documents = await (
                cursor.sort([("last_activity_at", DESCENDING), ("_id", DESCENDING)])
                .limit(page_size + 1)
                .batch_size(1)
                .to_list(length=page_size + 1)
            )
            has_more = len(documents) > page_size
            rows = tuple(
                self.__chat_session_from_document(document)
                for document in documents[:page_size]
            )
            if any(
                row is None
                or row.owner_user_id != owner_user_id
                or row.status != "active"
                or (row.expires_at is not None and row.expires_at <= now)
                or since is not None
                and row.last_activity_at < since
                or until is not None
                and row.last_activity_at >= until
                for row in rows
            ):
                raise ValueError("Review query returned an invalid chat")
            return rows, has_more
        except (AuthorizationDenied, AppDatabaseUnavailable):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Saved-chat review is unavailable.") from None
        finally:
            if cursor is not None:
                try:
                    await cursor.close()
                except Exception:
                    self.__poisoned = True
                    raise AppDatabaseUnavailable(
                        "Saved-chat review is unavailable."
                    ) from None

    @staticmethod
    def __chat_session_from_document(document: dict[str, Any] | None):
        if document is None:
            return None
        try:
            value = dict(document)
            validate_bson_document(value)
            value["id"] = value.pop("_id")
            for name in (
                "created_at",
                "updated_at",
                "last_activity_at",
                "expires_at",
            ):
                if isinstance(value.get(name), datetime):
                    value[name] = value[name].isoformat()
            context_state = value.get("context_state")
            if isinstance(context_state, dict):
                for name in ("start_utc", "end_utc"):
                    if isinstance(context_state.get(name), datetime):
                        context_state[name] = context_state[name].isoformat()
            attempt = value.get("last_attempt")
            if isinstance(attempt, dict):
                for name in ("started_at", "updated_at"):
                    if isinstance(attempt.get(name), datetime):
                        attempt[name] = attempt[name].isoformat()
            return ChatSessionRecord.model_validate_json(
                json.dumps(value, default=_json_datetime)
            )
        except Exception:
            raise AppDatabaseUnavailable("Saved-chat record is unavailable.") from None

    @staticmethod
    def __chat_turn_from_document(document: dict[str, Any] | None):
        if document is None:
            return None
        from goodshowroom_ai.storage.chat_turns import ChatTurnRecord

        try:
            value = dict(document)
            validate_bson_document(value)
            value["id"] = value.pop("_id")
            record = ChatTurnRecord.model_validate_json(
                json.dumps(value, default=_json_datetime)
            )
            if record.size_bytes != validate_bson_document(record.document()):
                raise ValueError("Saved-turn size metadata is inconsistent")
            return record
        except Exception:
            raise AppDatabaseUnavailable("Saved-turn record is unavailable.") from None

    async def list_active_mappings(self, user_id: UUID) -> tuple[IdentityMapping, ...]:
        self.__require(StorageCapability.READ_SETTINGS)
        if not isinstance(user_id, UUID) or user_id != self.__authorization.actor_id:
            raise AuthorizationDenied("Policy read is not authorized.")
        try:
            documents = (
                await self.__database["identity_mappings"]
                .find(
                    {"user_id": str(user_id), "active": True},
                    projection={"_id": 1},
                    session=self.__session,
                )
                .limit(101)
                .to_list(length=101)
            )
            if len(documents) > 100:
                raise ValueError()
            mappings = []
            for document in documents:
                mapping = await self.get_mapping(UUID(document["_id"]))
                if mapping is None or not mapping.active or mapping.user_id != user_id:
                    raise ValueError()
                mappings.append(mapping)
            return tuple(sorted(mappings, key=lambda m: str(m.id)))
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Policy is unavailable.") from None

    async def __read_identity(self, collection, identifier, model):
        if not isinstance(identifier, UUID) and not (
            model is AppSetting and identifier in {"sites", "retention", "disclosure"}
        ):
            raise ValueError("Identity ID must be a UUID")
        try:
            document = await self.__database[collection].find_one(
                {"_id": str(identifier)}, session=self.__session
            )
            if document is None:
                return None
            document = dict(document)
            validate_bson_document(document)
            document["id"] = document.pop("_id")

            # JSON mode parses canonical stored UUID strings without relaxing strict models.
            for key in (
                "created_at",
                "updated_at",
                "expires_at",
                "reviewed_at",
                "auth_expires_at",
                "auth_started_at",
                "lease_valid_until",
                "revoked_at",
                "purge_after",
                "verified_at",
            ):
                if isinstance(document.get(key), datetime):
                    document[key] = document[key].isoformat()
            return model.model_validate_json(json.dumps(document))
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Identity record is unavailable.") from None

    async def get_mapping(self, identifier: UUID) -> IdentityMapping | None:
        self.__require(StorageCapability.READ_SETTINGS)
        return await self.__read_identity(
            "identity_mappings", identifier, IdentityMapping
        )

    async def get_setting(self, name: str) -> AppSetting | None:
        self.__require(StorageCapability.READ_SETTINGS)
        if not isinstance(name, str) or name not in {
            "sites",
            "retention",
            "disclosure",
        }:
            raise ValueError("Unknown setting")
        return await self.__read_identity("app_settings", name, AppSetting)

    async def save_mapping(
        self, record: IdentityMapping, expected_version: int | None = None
    ) -> None:
        self.__require(StorageCapability.WRITE_SETTINGS)
        self.__access_write_ready()
        try:
            if (
                not isinstance(record, IdentityMapping)
                or record.verified_by != self.__authorization.actor_id
            ):
                raise ValueError("Verified service mapping required")
            if await self.__read_identity("users", record.user_id, UserRecord) is None:
                raise ValueError("Mapping parent is unavailable")
            if expected_version is not None:
                current = await self.__read_identity(
                    "identity_mappings", record.id, IdentityMapping
                )
                if (
                    current is None
                    or current.user_id != record.user_id
                    or current.source_ref != record.source_ref
                    or record.mapping_version != current.mapping_version + 1
                    or record.verified_at < current.verified_at
                ):
                    raise AppWriteConflict("Mapping binding or version changed.")
            elif record.mapping_version != 1:
                raise ValueError("New mapping version must be one")
            await self.__write_identity(
                "identity_mappings",
                record,
                expected_version,
                capability=StorageCapability.WRITE_SETTINGS,
            )
        except (ValueError, AppWriteConflict):
            self.__poisoned = True
            raise

    async def save_setting(
        self, record: AppSetting, expected_version: int | None = None
    ) -> None:
        self.__require(StorageCapability.WRITE_SETTINGS)
        if not isinstance(record, AppSetting):
            raise ValueError("Typed setting required")
        await self.__write_identity(
            "app_settings",
            record,
            expected_version,
            capability=StorageCapability.WRITE_SETTINGS,
        )

    async def get_login(self, identifier: UUID) -> LoginRecord | None:
        self.__require(StorageCapability.READ_LOGINS)
        record = await self.__read_identity("login_sessions", identifier, LoginRecord)
        if record is not None and record.user_id != self.__authorization.actor_id:
            raise AuthorizationDenied("Login is not authorized.")
        return record

    async def exchange_login(self, record: LoginRecord, now: datetime) -> LoginRecord:
        self.__require(StorageCapability.WRITE_LOGINS)
        self.__access_write_ready()
        try:
            if (
                not isinstance(record, LoginRecord)
                or record.user_id != self.__authorization.actor_id
            ):
                raise ValueError("Invalid new login binding")
            if await self.__read_identity("users", record.user_id, UserRecord) is None:
                raise ValueError("Login parent is unavailable")
            current = await self.__read_identity(
                "login_sessions", record.id, LoginRecord
            )
            if current is not None:
                if (
                    current.user_id != record.user_id
                    or current.auth_expires_at != record.auth_expires_at
                    or current.auth_started_at != record.auth_started_at
                    or not current.valid(now)
                ):
                    raise AppWriteConflict("Login binding is unavailable.")
                return current
            if (
                record.version != 0
                or record.generation != 0
                or record.created_at != now
                or record.updated_at != now
                or not record.valid(now)
            ):
                raise ValueError("Invalid new login binding")
            await self.__database["login_sessions"].insert_one(
                record.document(), session=self.__session
            )
            return record
        except (ValueError, AppWriteConflict):
            self.__poisoned = True
            raise
        except DuplicateKeyError:
            self.__poisoned = True
            raise AppWriteConflict(
                "Login exchange changed; retry in a new transaction."
            ) from None
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Login exchange failed.") from None

    async def renew_login(
        self,
        identifier: UUID,
        *,
        generation: int,
        expected_version: int,
        now: datetime,
        verified_auth_expiry: datetime,
    ) -> LoginRecord:
        if (
            not isinstance(verified_auth_expiry, datetime)
            or verified_auth_expiry.tzinfo is None
        ):
            raise ValueError("Verified authentication expiry is required")
        return await self.__change_login(
            identifier, generation, expected_version, now, verified_auth_expiry
        )

    async def revoke_login(
        self, identifier: UUID, *, generation: int, expected_version: int, now: datetime
    ) -> LoginRecord:
        return await self.__change_login(
            identifier, generation, expected_version, now, None
        )

    async def __change_login(
        self, identifier, generation, expected_version, now, auth_expiry
    ):
        self.__require(StorageCapability.WRITE_LOGINS)
        self.__access_write_ready()
        try:
            current = await self.__read_identity(
                "login_sessions", identifier, LoginRecord
            )
            if current is None or current.user_id != self.__authorization.actor_id:
                raise AuthorizationDenied("Login is not authorized.")
            if type(generation) is not int or generation != current.generation:
                raise AppWriteConflict("Login generation changed.")
            if auth_expiry is None and current.revoked_at is not None:
                return current
            if (
                type(expected_version) is not int
                or expected_version != current.version
                or now < current.updated_at
            ):
                raise AppWriteConflict("Login version changed.")
            if auth_expiry is not None and (
                auth_expiry != current.auth_expires_at or not current.valid(now)
            ):
                raise AppWriteConflict("Login cannot be renewed.")
            values = current.model_dump()
            values.update(version=current.version + 1, updated_at=now)
            if auth_expiry is None:
                values["revoked_at"] = now
            else:
                values["lease_valid_until"] = min(
                    now + timedelta(minutes=5), current.expires_at
                )
            updated = LoginRecord.model_validate(values)
            result = await self.__database["login_sessions"].replace_one(
                {
                    "_id": str(identifier),
                    "version": expected_version,
                    "generation": generation,
                    "revoked_at": None,
                },
                updated.document(),
                session=self.__session,
            )
            if result.matched_count != 1:
                raise AppWriteConflict("Login changed.")
            await self.__advance_policy_version()
            return updated
        except (ValueError, AppWriteConflict, AuthorizationDenied):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Login change failed.") from None

    async def get_access_request(self, identifier: UUID) -> AccessRequestRecord | None:
        self.__require(StorageCapability.READ_ACCESS_REQUESTS)
        return await self.__read_identity(
            "access_requests", identifier, AccessRequestRecord
        )

    async def list_access_requests(
        self, *, status: str, limit: int = 50, after_id: UUID | None = None
    ) -> tuple[AccessRequestRecord, ...]:
        self.__require(StorageCapability.READ_ACCESS_REQUESTS)
        if (
            status not in {"pending", "approved", "rejected", "consumed", "revoked"}
            or type(limit) is not int
            or not 1 <= limit <= 100
            or (after_id is not None and not isinstance(after_id, UUID))
        ):
            raise ValueError("Invalid access request page")
        query = {"status": status}
        if after_id is not None:
            query["_id"] = {"$gt": str(after_id)}
        try:
            cursor = (
                self.__database["access_requests"]
                .find(query, session=self.__session)
                .sort("_id", 1)
                .limit(limit)
                .max_time_ms(5000)
            )
            rows = await cursor.to_list(length=limit)
            results = []
            total_bytes = 0
            for row in rows:
                total_bytes += validate_bson_document(row)
                if total_bytes > MAX_APP_DOCUMENT_BYTES:
                    raise ValueError("Access request page exceeds storage bounds")
                record = await self.__read_identity(
                    "access_requests", UUID(row["_id"]), AccessRequestRecord
                )
                if record is None:
                    raise AppWriteConflict("Access request changed.")
                results.append(record)
            return tuple(results)
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable(
                "Access request page is unavailable."
            ) from None

    async def create_access_request(
        self, record: AccessRequestRecord, now: datetime
    ) -> AccessRequestRecord:
        """Recover pending duplicates; never resubmit rejected or revoked entries."""
        self.__require(StorageCapability.WRITE_ACCESS_REQUESTS)
        self.__access_write_ready()
        if (
            not isinstance(record, AccessRequestRecord)
            or record.status != "pending"
            or not record.available(now)
        ):
            raise ValueError("New access record must be available and pending")
        try:
            if record.kind == "request":
                user = await self.__read_identity("users", record.user_id, UserRecord)
                if (
                    user is None
                    or user.email_key != record.email_key
                    or user.approval_status != "pending"
                    or user.id != self.__authorization.actor_id
                ):
                    raise ValueError("Request parent is not eligible")
            else:
                role = await self.__read_identity("roles", record.role_id, RoleRecord)
                if (
                    role is None
                    or not role.active
                    or any(
                        not any(scope_within(grant, allowed) for allowed in role.grants)
                        for grant in record.scope_limit
                    )
                ):
                    raise ValueError("Preapproval role or scope is not permitted")
            query = {"kind": record.kind, "email_key": record.email_key}
            existing = await self.__database["access_requests"].find_one(
                {**query, "active": True}, session=self.__session
            )
            if existing is not None:
                previous = await self.__read_identity(
                    "access_requests", UUID(existing["_id"]), AccessRequestRecord
                )
                if (
                    previous is not None
                    and previous.available(now)
                    and previous.user_id == record.user_id
                    and previous.role_id == record.role_id
                    and previous.scope_limit == record.scope_limit
                ):
                    return previous
                raise AppWriteConflict("Access request already exists.")
            history = await self.__database["access_requests"].find_one(
                query, session=self.__session
            )
            if history is not None:
                raise AppWriteConflict("Resubmission requires an approved policy.")
            await self.__database["access_requests"].insert_one(
                record.document(), session=self.__session
            )
            await self.__advance_policy_version()
            return record
        except (ValueError, AppWriteConflict):
            self.__poisoned = True
            raise
        except DuplicateKeyError:
            self.__poisoned = True
            raise AppWriteConflict(
                "Access request changed; retry in a new transaction."
            ) from None
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Access request write failed.") from None

    async def close_access_request(
        self, identifier: UUID, *, status: str, now: datetime
    ) -> None:
        """Only rejection/revocation storage. Approval and consumption are service-owned."""
        self.__require(StorageCapability.WRITE_ACCESS_REQUESTS)
        self.__access_write_ready()
        if status not in {"rejected", "revoked"}:
            raise ValueError("Approval and consumption require their service workflow")
        try:
            current = await self.__read_identity(
                "access_requests", identifier, AccessRequestRecord
            )
            if (
                current is None
                or not current.available(now)
                or (current.kind == "preapproval" and status != "revoked")
            ):
                raise AppWriteConflict("Access request is not available.")
            updated = AccessRequestRecord.model_validate(
                {
                    **current.model_dump(),
                    "status": status,
                    "active": False,
                    "updated_at": now,
                    "reviewed_at": now,
                    "reviewed_by": self.__authorization.actor_id,
                }
            )
            result = await self.__database["access_requests"].replace_one(
                {"_id": str(identifier), "status": "pending", "active": True},
                updated.document(),
                session=self.__session,
            )
            if result.matched_count != 1:
                raise AppWriteConflict("Access request changed.")
            await self.__advance_policy_version()
        except (ValueError, AppWriteConflict):
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Access request write failed.") from None

    def __access_write_ready(self):
        if not self.__fence_advanced or self.__audit_written:
            raise ValueError("Access writes require a fence before audit")

    async def __advance_policy_version(self):
        if not self.__identity_changed:
            result = await self.__database["app_settings"].update_one(
                {"_id": _ACCESS_CONTROL_SETTINGS_ID},
                {"$inc": {"policy_version": 1}},
                session=self.__session,
            )
            if result.matched_count != 1:
                raise AppWriteConflict("Access-control state is unavailable.")
            self.__identity_changed = True

    async def create_user(self, record: UserRecord) -> None:
        if (
            not isinstance(record, UserRecord)
            or record.approval_status != "pending"
            or record.role_id is not None
            or record.scope_limit
        ):
            raise ValueError("New identities must be pending without grants")
        await self.__write_identity("users", record, None)

    async def create_role(self, record: RoleRecord) -> None:
        if not isinstance(record, RoleRecord):
            raise ValueError("Role record required")
        await self.__write_identity("roles", record, None)

    async def update_user(self, record: UserRecord, expected_version: int) -> None:
        """Persist profile edits without rebinding identity or changing access."""
        self.__require(StorageCapability.WRITE_IDENTITIES)
        if not isinstance(record, UserRecord):
            raise ValueError("User record required")
        current = await self.__read_identity("users", record.id, UserRecord)
        if current is None:
            raise AppWriteConflict("Identity version changed.")
        if (
            record.provider,
            record.provider_account_id,
            record.approval_status,
            record.role_id,
            record.scope_limit,
        ) != (
            current.provider,
            current.provider_account_id,
            current.approval_status,
            current.role_id,
            current.scope_limit,
        ):
            raise ValueError(
                "Identity binding and access cannot change in profile edits"
            )
        await self.__write_identity("users", record, expected_version)

    async def update_role(self, record: RoleRecord, expected_version: int) -> None:
        if not isinstance(record, RoleRecord):
            raise ValueError("Role record required")
        await self.__write_identity("roles", record, expected_version)

    async def __write_identity(
        self,
        collection,
        record,
        expected_version,
        *,
        capability=StorageCapability.WRITE_IDENTITIES,
    ):
        self.__require(capability)
        if not self.__fence_advanced or self.__audit_written:
            raise ValueError("Identity writes require a fence before audit")
        if expected_version is None:
            if record.version != 0:
                raise ValueError("New records start at version zero")
        elif (
            type(expected_version) is not int
            or expected_version < 0
            or record.version != expected_version + 1
        ):
            raise ValueError("Invalid expected version")
        document = record.document()
        try:
            if expected_version is None:
                await self.__database[collection].insert_one(
                    document, session=self.__session
                )
            else:
                result = await self.__database[collection].replace_one(
                    {
                        "_id": str(record.id),
                        "version": expected_version,
                        "created_at": record.created_at,
                    },
                    document,
                    session=self.__session,
                )
                if result.matched_count != 1:
                    raise AppWriteConflict("Identity version changed.")
            if not self.__identity_changed:
                policy_result = await self.__database["app_settings"].update_one(
                    {"_id": _ACCESS_CONTROL_SETTINGS_ID},
                    {"$inc": {"policy_version": 1}},
                    session=self.__session,
                )
                if policy_result.matched_count != 1:
                    raise AppWriteConflict("Access-control state is unavailable.")
                self.__identity_changed = True
        except DuplicateKeyError:
            self.__poisoned = True
            raise AppWriteConflict("Identity already exists.") from None
        except AppWriteConflict:
            self.__poisoned = True
            raise
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Identity write failed.") from None

    async def advance_access_control_fence(self, expected_policy_version: int) -> None:
        self.__require(StorageCapability.ADVANCE_ACCESS_CONTROL)
        if self.__fence_advanced:
            raise ValueError(
                "Only one access-control update is allowed per transaction"
            )
        if type(expected_policy_version) is not int or expected_policy_version < 0:
            raise ValueError("Policy version must be a non-negative integer")
        settings = self.__database["app_settings"]
        try:
            result = await settings.update_one(
                {
                    "_id": _ACCESS_CONTROL_SETTINGS_ID,
                    "policy_version": expected_policy_version,
                },
                {"$inc": {"commit_fence": 1}},
                session=self.__session,
            )
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("App database write failed.") from None
        if result.matched_count != 1:
            self.__poisoned = True
            raise AppWriteConflict("Access-control policy changed.")
        self.__fence_advanced = True

    async def append_required_audit(self, event: AppAuditEvent) -> None:
        self.__require(StorageCapability.WRITE_REQUIRED_AUDIT)
        if not self.__fence_advanced or self.__audit_written:
            raise ValueError("Audit must follow one protected access-control update")
        if not isinstance(event, AppAuditEvent):
            raise ValueError("Audit event must use the approved storage model")
        if event.actor_id != self.__authorization.actor_id:
            raise AuthorizationDenied("App write is not authorized.")
        document = event.to_bson_document()
        validate_bson_document(document)
        try:
            await self.__database["audit_events"].insert_one(
                document, session=self.__session
            )
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Required audit write failed.") from None
        self.__audit_written = True

    def _verify_can_commit(self) -> None:
        if self.__poisoned:
            raise AppDatabaseUnavailable("App transaction is not safe to commit.")
        if self.__fence_advanced and not self.__audit_written:
            raise RequiredAuditMissing("Required audit event is missing.")

    def _deactivate(self) -> None:
        self.__active = False

    def __require_review_access(self) -> None:
        self.__require(StorageCapability.READ_CHATS)
        if self.__review_authorized is not True:
            raise AuthorizationDenied("Saved-chat review is not authorized.")

    def __require(self, capability: StorageCapability) -> None:
        if not self.__active:
            raise AppDatabaseUnavailable("App transaction is no longer active.")
        if not self.__authorization.permits(capability):
            raise AuthorizationDenied("App write is not authorized.")
