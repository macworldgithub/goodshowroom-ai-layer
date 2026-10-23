"""Fixed-target Mongo lifecycle and app-only write primitives."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from goodshowroom_ai.storage.logins import LoginRecord
from typing import Any, AsyncContextManager
from uuid import UUID

from bson.codec_options import CodecOptions
from pydantic import SecretStr
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError
from goodshowroom_ai.storage.identities import UserRecord, RoleRecord
from goodshowroom_ai.storage.access_requests import AccessRequestRecord, scope_within

from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    MAX_APP_DOCUMENT_BYTES,
    InternalAuthorizationContext,
    StorageCapability,
    validate_bson_document,
)

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


class AppDatabaseUnavailable(RuntimeError):
    """The app database is not configured or cannot be reached."""


class AuthorizationDenied(PermissionError):
    """A typed app write context did not authorize the requested operation."""


class AppWriteConflict(RuntimeError):
    """The access-control policy version did not match the expected version."""


class RequiredAuditMissing(RuntimeError):
    """A protected access-control write did not add its required audit event."""


ClientFactory = Callable[..., Any]


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

    @asynccontextmanager
    async def __write_transaction(
        self, context: InternalAuthorizationContext
    ) -> AsyncIterator["_AppWriteTransaction"]:
        database = self.__require_database()
        transaction = None
        try:
            async with self.__client.start_session().bind() as session:
                async with session.start_transaction():
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
        except Exception:
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

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        self.__require(StorageCapability.READ_IDENTITIES)
        return await self.__read_identity("users", user_id, UserRecord)

    async def get_role(self, role_id: UUID) -> RoleRecord | None:
        self.__require(StorageCapability.READ_IDENTITIES)
        return await self.__read_identity("roles", role_id, RoleRecord)

    async def __read_identity(self, collection, identifier, model):
        if not isinstance(identifier, UUID):
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
            ):
                if isinstance(document.get(key), datetime):
                    document[key] = document[key].isoformat()
            return model.model_validate_json(json.dumps(document))
        except Exception:
            self.__poisoned = True
            raise AppDatabaseUnavailable("Identity record is unavailable.") from None

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

    async def __write_identity(self, collection, record, expected_version):
        self.__require(StorageCapability.WRITE_IDENTITIES)
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

    def __require(self, capability: StorageCapability) -> None:
        if not self.__active:
            raise AppDatabaseUnavailable("App transaction is no longer active.")
        if not self.__authorization.permits(capability):
            raise AuthorizationDenied("App write is not authorized.")
