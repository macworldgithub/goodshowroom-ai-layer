"""Offline bounded population storage checks for role and user access management."""

from uuid import UUID, uuid4
import pytest

from goodshowroom_ai.storage.models import (
    InternalAuthorizationContext,
    StorageCapability,
)
from goodshowroom_ai.storage.mongo import (
    AppDatabase,
    AppDatabaseUnavailable,
)
from tests.test_identity_storage import role_record, user_record
from tests.workflow_fake import RollbackClient


def _context():
    return InternalAuthorizationContext(
        actor_id=uuid4(),
        capabilities={
            StorageCapability.READ_IDENTITIES,
            StorageCapability.WRITE_IDENTITIES,
            StorageCapability.READ_POLICY,
            StorageCapability.ADVANCE_ACCESS_CONTROL,
            StorageCapability.WRITE_REQUIRED_AUDIT,
        },
    )


@pytest.mark.asyncio
async def test_access_management_roles_bounded_population_and_ordering():
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    # Seed 5 roles out of order
    role_ids = [UUID(int=i) for i in (5, 2, 8, 1, 3)]
    for r_id in role_ids:
        r = role_record(name_key=f"role_{r_id.int}", active=True).model_copy(
            update={"id": r_id}
        )
        client.database["roles"].documents[str(r_id)] = r.document()

    async with db.transaction(_context()) as tx:
        roles = await tx.access_management_roles()

    assert len(roles) == 5
    # Must be ordered by _id ascending
    assert [r.id for r in roles] == sorted(role_ids, key=str)


@pytest.mark.asyncio
async def test_access_management_roles_fails_when_population_exceeds_1000():
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    for i in range(1001):
        r_id = UUID(int=i + 1)
        r = role_record(name_key=f"role_{i}", active=True).model_copy(
            update={"id": r_id}
        )
        client.database["roles"].documents[str(r_id)] = r.document()

    with pytest.raises(AppDatabaseUnavailable):
        async with db.transaction(_context()) as tx:
            await tx.access_management_roles()


@pytest.mark.asyncio
async def test_access_management_roles_fails_on_corrupt_or_missing_record():
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    r_id = uuid4()
    # Malformed document
    client.database["roles"].documents[str(r_id)] = {
        "_id": str(r_id),
        "corrupt": True,
    }

    with pytest.raises(AppDatabaseUnavailable):
        async with db.transaction(_context()) as tx:
            await tx.access_management_roles()


@pytest.mark.asyncio
async def test_access_management_users_bounded_population_and_ordering():
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    user_ids = [UUID(int=i) for i in (7, 3, 9, 2)]
    for u_id in user_ids:
        u = user_record(
            email_key=f"user_{u_id.int}@example.test",
            provider_account_id=f"prov_{u_id.int}",
        ).model_copy(update={"id": u_id})
        client.database["users"].documents[str(u_id)] = u.document()

    async with db.transaction(_context()) as tx:
        users = await tx.access_management_users()

    assert len(users) == 4
    assert [u.id for u in users] == sorted(user_ids, key=str)


@pytest.mark.asyncio
async def test_access_management_users_fails_when_population_exceeds_1000():
    client = RollbackClient([])
    db = await AppDatabase.connect(
        "mongodb://offline.invalid", client_factory=lambda *_a, **_k: client
    )
    for i in range(1001):
        u_id = UUID(int=i + 1)
        u = user_record(
            email_key=f"user_{i}@example.test",
            provider_account_id=f"prov_{i}",
        ).model_copy(update={"id": u_id})
        client.database["users"].documents[str(u_id)] = u.document()

    with pytest.raises(AppDatabaseUnavailable):
        async with db.transaction(_context()) as tx:
            await tx.access_management_users()
