"""Offline storage contracts for TICKET-024 login validity and revocation."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from goodshowroom_ai.storage.logins import LoginRecord
from goodshowroom_ai.storage.models import StorageCapability
from goodshowroom_ai.storage.mongo import (
    APP_DATABASE_NAME,
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
    RequiredAuditMissing,
)
from tests.test_identity_storage import (
    NOW,
    POLICY_VERSION,
    audit,
    connect_fake,
    login_context,
    login_record,
    user_record,
)


def test_login_absolute_expiry_uses_auth_start_and_auth_expiry_independently():
    actor_id = uuid4()
    auth_started_at = NOW - timedelta(hours=1)
    latest_expiry = login_record(
        actor_id,
        auth_started_at=auth_started_at,
        auth_expires_at=NOW + timedelta(hours=9),
        expires_at=NOW + timedelta(hours=7),
    )
    assert latest_expiry.expires_at == auth_started_at + timedelta(hours=8)

    with pytest.raises(ValidationError):
        login_record(
            actor_id,
            auth_started_at=auth_started_at,
            auth_expires_at=NOW + timedelta(hours=9),
            expires_at=NOW + timedelta(hours=7, microseconds=1),
        )

    auth_limited = login_record(
        actor_id,
        auth_expires_at=NOW + timedelta(hours=2),
        expires_at=NOW + timedelta(hours=2),
    )
    assert auth_limited.expires_at == auth_limited.auth_expires_at
    with pytest.raises(ValidationError):
        login_record(
            actor_id,
            auth_expires_at=NOW + timedelta(hours=2),
            expires_at=NOW + timedelta(hours=2, microseconds=1),
        )
    with pytest.raises(ValidationError):
        login_record(actor_id, auth_started_at=NOW + timedelta(microseconds=1))


def test_login_lease_is_limited_by_five_minutes_and_absolute_expiry():
    actor_id = uuid4()
    bounded = login_record(actor_id)
    assert bounded.lease_valid_until == NOW + timedelta(minutes=5)
    assert bounded.valid(bounded.lease_valid_until - timedelta(microseconds=1))
    assert not bounded.valid(bounded.lease_valid_until)

    with pytest.raises(ValidationError):
        login_record(
            actor_id,
            lease_valid_until=NOW + timedelta(minutes=5, microseconds=1),
        )
    with pytest.raises(ValidationError):
        login_record(
            actor_id,
            expires_at=NOW + timedelta(minutes=4),
            lease_valid_until=NOW + timedelta(minutes=4, microseconds=1),
        )


def test_login_replay_binding_is_retained_for_assertion_expiry_and_tolerance():
    actor_id = uuid4()
    short_login = login_record(
        actor_id,
        auth_expires_at=NOW + timedelta(minutes=10),
        expires_at=NOW + timedelta(minutes=1),
        lease_valid_until=NOW + timedelta(minutes=1),
    )
    assert short_login.purge_after == short_login.expires_at + timedelta(seconds=150)
    with pytest.raises(ValidationError):
        login_record(
            actor_id,
            auth_expires_at=NOW + timedelta(minutes=10),
            expires_at=NOW + timedelta(minutes=1),
            lease_valid_until=NOW + timedelta(minutes=1),
            purge_after=NOW + timedelta(minutes=3, seconds=29),
        )
    forbidden = {"raw_cookie", "google_access_token", "internal_token"}
    assert forbidden.isdisjoint(LoginRecord.model_fields)
    assert forbidden.isdisjoint(short_login.document())
    with pytest.raises(ValidationError):
        login_record(uuid4(), raw_cookie="not-stored")


@pytest.mark.asyncio
async def test_login_indexes_are_explicit_app_only_and_retain_replay_bindings():
    database, client, _events = await connect_fake()
    await database.initialize_login_indexes()

    indexes = {
        options["name"]: (keys, options)
        for keys, options in client.database["login_sessions"].indexes
    }
    assert set(indexes) == {"ix_user_revoked", "ttl_login_purge"}
    assert indexes["ix_user_revoked"][0] == (
        ("user_id", 1),
        ("revoked_at", 1),
    )
    assert indexes["ttl_login_purge"] == (
        (("purge_after", 1),),
        {"expireAfterSeconds": 0, "name": "ttl_login_purge"},
    )
    assert client.selected_databases == [APP_DATABASE_NAME]
    assert set(client.database.collections) == {"app_settings", "login_sessions"}


@pytest.mark.asyncio
async def test_login_reads_require_capability_and_are_owner_scoped():
    database, client, _events = await connect_fake()
    owner_id = uuid4()
    record = login_record(owner_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()

    async with database.transaction(
        login_context(owner_id, capabilities={StorageCapability.READ_LOGINS})
    ) as transaction:
        assert await transaction.get_login(record.id) == record

    with pytest.raises(AuthorizationDenied, match="Login is not authorized"):
        async with database.transaction(
            login_context(uuid4(), capabilities={StorageCapability.READ_LOGINS})
        ) as transaction:
            await transaction.get_login(record.id)

    with pytest.raises(AuthorizationDenied):
        async with database.transaction(
            login_context(owner_id, capabilities={StorageCapability.WRITE_LOGINS})
        ) as transaction:
            await transaction.get_login(record.id)


@pytest.mark.asyncio
async def test_login_exchange_requires_write_capability_and_fence():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)

    with pytest.raises(AuthorizationDenied):
        async with database.transaction(
            login_context(
                actor_id,
                capabilities={
                    StorageCapability.ADVANCE_ACCESS_CONTROL,
                    StorageCapability.WRITE_REQUIRED_AUDIT,
                },
            )
        ) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.exchange_login(record, NOW)
    assert "login_sessions" not in client.database.collections

    with pytest.raises(ValueError, match="fence before audit"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.exchange_login(record, NOW)
    assert "login_sessions" not in client.database.collections


@pytest.mark.asyncio
async def test_login_exchange_requires_parent_and_replay_does_not_extend_lease():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    user = user_record(id=actor_id)
    client.database["users"].documents[str(user.id)] = user.document()
    original = login_record(actor_id)

    async with database.transaction(login_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION)
        created = await transaction.exchange_login(original, NOW)
        await transaction.append_required_audit(audit(actor_id, original.id))

    renew_at = NOW + timedelta(minutes=2)
    async with database.transaction(login_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION)
        renewed = await transaction.renew_login(
            original.id,
            generation=original.generation,
            expected_version=original.version,
            now=renew_at,
            verified_auth_expiry=original.auth_expires_at,
        )
        await transaction.append_required_audit(audit(actor_id, original.id))

    retry_at = NOW + timedelta(minutes=3)
    async with database.transaction(login_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION + 1)
        replayed = await transaction.exchange_login(original, retry_at)
        await transaction.append_required_audit(audit(actor_id, original.id))

    assert created == original
    assert renewed.version == 1
    assert renewed.lease_valid_until == renew_at + timedelta(minutes=5)
    assert replayed == renewed
    assert replayed.lease_valid_until == renewed.lease_valid_until
    assert [write[0] for write in client.database["login_sessions"].writes] == [
        "insert",
        "replace",
    ]

    empty_database, empty_client, events = await connect_fake()
    with pytest.raises(ValueError, match="parent is unavailable"):
        async with empty_database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.exchange_login(original, NOW)
    assert "login_sessions" not in empty_client.database.collections
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", ["expired_lease", "revoked"])
async def test_login_exchange_cannot_recreate_expired_or_revoked_binding(invalid_state):
    database, client, events = await connect_fake()
    actor_id = uuid4()
    user = user_record(id=actor_id)
    client.database["users"].documents[str(user.id)] = user.document()
    if invalid_state == "expired_lease":
        existing = login_record(actor_id, lease_valid_until=NOW + timedelta(minutes=1))
        retry_at = NOW + timedelta(minutes=1)
    else:
        retry_at = NOW + timedelta(minutes=1)
        existing = login_record(
            actor_id,
            updated_at=retry_at,
            revoked_at=retry_at,
        )
    client.database["login_sessions"].documents[str(existing.id)] = existing.document()
    candidate = login_record(
        actor_id,
        id=existing.id,
        auth_started_at=existing.auth_started_at,
        auth_expires_at=existing.auth_expires_at,
        expires_at=existing.expires_at,
        created_at=retry_at,
        updated_at=retry_at,
        lease_valid_until=retry_at + timedelta(minutes=5),
        purge_after=existing.purge_after,
    )

    with pytest.raises(AppWriteConflict, match="Login binding is unavailable"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.exchange_login(candidate, retry_at)
    assert len(client.database["login_sessions"].writes) == 0
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_login_renewal_extends_only_the_lease_and_uses_version_cas():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()
    renew_at = NOW + timedelta(minutes=4)

    async with database.transaction(login_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION)
        renewed = await transaction.renew_login(
            record.id,
            generation=record.generation,
            expected_version=record.version,
            now=renew_at,
            verified_auth_expiry=record.auth_expires_at,
        )
        await transaction.append_required_audit(audit(actor_id, record.id))

    assert renewed.version == record.version + 1
    assert renewed.updated_at == renew_at
    assert renewed.lease_valid_until == renew_at + timedelta(minutes=5)
    assert renewed.expires_at == record.expires_at
    assert renewed.auth_expires_at == record.auth_expires_at
    assert renewed.purge_after == record.purge_after
    cas = client.database["login_sessions"].writes[-1]
    assert cas[0] == "replace"
    assert cas[1] == {
        "_id": str(record.id),
        "version": record.version,
        "generation": record.generation,
        "revoked_at": None,
    }
    assert cas[-1] is client.session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state", ["expired_lease", "expired_absolute", "revoked"]
)
async def test_login_renewal_cannot_revive_expired_or_revoked_generation(invalid_state):
    database, client, events = await connect_fake()
    actor_id = uuid4()
    if invalid_state == "expired_lease":
        record = login_record(actor_id, lease_valid_until=NOW + timedelta(minutes=5))
        now = record.lease_valid_until
    elif invalid_state == "expired_absolute":
        expiry = NOW + timedelta(hours=8)
        record = login_record(
            actor_id,
            expires_at=expiry,
            updated_at=expiry - timedelta(minutes=5),
            lease_valid_until=expiry,
        )
        now = expiry
    else:
        revoked_at = NOW + timedelta(minutes=1)
        record = login_record(
            actor_id,
            updated_at=revoked_at,
            revoked_at=revoked_at,
        )
        now = revoked_at
    client.database["login_sessions"].documents[str(record.id)] = record.document()

    with pytest.raises(AppWriteConflict, match="Login cannot be renewed"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.renew_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=now,
                verified_auth_expiry=record.auth_expires_at,
            )
    assert client.database["login_sessions"].writes == []
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_login_renewal_requires_the_same_aware_originating_auth_expiry():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()

    with pytest.raises(AppWriteConflict, match="Login cannot be renewed"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.renew_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
                verified_auth_expiry=record.auth_expires_at + timedelta(seconds=1),
            )
    with pytest.raises(ValueError, match="Verified authentication expiry"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.renew_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
                verified_auth_expiry=None,
            )

    assert client.database["login_sessions"].writes == []


@pytest.mark.asyncio
async def test_login_revocation_is_idempotent_and_preserves_expiry_and_replay_retention():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()
    revoked_at = NOW + timedelta(minutes=1)

    async with database.transaction(login_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(POLICY_VERSION)
        first = await transaction.revoke_login(
            record.id,
            generation=record.generation,
            expected_version=record.version,
            now=revoked_at,
        )
        await transaction.append_required_audit(audit(actor_id, record.id))

    next_policy_version = client.database["app_settings"].documents["access_control"][
        "policy_version"
    ]
    async with database.transaction(login_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(next_policy_version)
        repeated = await transaction.revoke_login(
            record.id,
            generation=record.generation,
            expected_version=record.version,
            now=revoked_at + timedelta(minutes=1),
        )
        await transaction.append_required_audit(audit(actor_id, record.id))

    assert first.version == 1
    assert first.revoked_at == revoked_at
    assert repeated == first
    assert repeated.expires_at == record.expires_at
    assert repeated.purge_after == record.purge_after
    assert len(client.database["login_sessions"].writes) == 1
    assert (
        client.database["app_settings"].documents["access_control"]["policy_version"]
        == POLICY_VERSION + 1
    )
    assert (
        client.database["app_settings"].documents["access_control"]["commit_fence"] == 2
    )
    assert len(client.database["audit_events"].writes) == 2
    fence_writes = [
        write
        for write in client.database["app_settings"].writes
        if write[2] == {"$inc": {"commit_fence": 1}}
    ]
    assert len(fence_writes) == 2
    assert all(write[-1] is client.session for write in fence_writes)
    assert all(
        write[-1] is client.session for write in client.database["audit_events"].writes
    )
    assert all(
        write[-1] is client.session
        for write in client.database["login_sessions"].writes
    )


@pytest.mark.asyncio
async def test_login_revocation_cas_conflict_aborts_before_policy_change():
    database, client, events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()
    client.database["login_sessions"].reject_replace = True

    with pytest.raises(AuthorizationDenied, match="Login is not authorized"):
        async with database.transaction(login_context(uuid4())) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.revoke_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
            )
    with pytest.raises(AppWriteConflict, match="Login generation changed"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.revoke_login(
                record.id,
                generation=record.generation + 1,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
            )
    assert client.database["login_sessions"].writes == []

    with pytest.raises(AppWriteConflict, match="Login changed"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.revoke_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
            )

    cas = client.database["login_sessions"].writes[-1]
    assert cas[1] == {
        "_id": str(record.id),
        "version": record.version,
        "generation": record.generation,
        "revoked_at": None,
    }
    assert (
        client.database["app_settings"].documents["access_control"]["policy_version"]
        == POLICY_VERSION
    )
    assert (
        client.database["login_sessions"].documents[str(record.id)] == record.document()
    )
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_login_revocation_audit_failure_aborts_the_transaction():
    database, client, events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()
    client.database["audit_events"].fail_insert = True

    with pytest.raises(AppDatabaseUnavailable, match="Required audit write failed"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.revoke_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
            )
            await transaction.append_required_audit(audit(actor_id, record.id))

    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_login_revocation_cannot_commit_without_required_audit():
    database, client, events = await connect_fake()
    actor_id = uuid4()
    record = login_record(actor_id)
    client.database["login_sessions"].documents[str(record.id)] = record.document()

    with pytest.raises(RequiredAuditMissing, match="Required audit event is missing"):
        async with database.transaction(login_context(actor_id)) as transaction:
            await transaction.advance_access_control_fence(POLICY_VERSION)
            await transaction.revoke_login(
                record.id,
                generation=record.generation,
                expected_version=record.version,
                now=NOW + timedelta(minutes=1),
            )

    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]
