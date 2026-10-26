"""Offline contracts for verified mappings and typed app settings."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.identities import SourceReference
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
)
from goodshowroom_ai.storage.mongo import (
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
    RequiredAuditMissing,
)
from goodshowroom_ai.storage.settings import (
    AppSetting,
    IdentityMapping,
    SiteConfiguration,
)
from tests.test_identity_storage import (
    NOW,
    POLICY_VERSION,
    audit,
    connect_fake,
    full_context,
    user_record,
)


@pytest.mark.parametrize("blank_key", ["", "   "])
def test_blank_cursor_key_leaves_cursor_codec_unconfigured(blank_key):
    settings = Settings(
        _env_file=None,
        app_env="test",
        AI_LAYER_CURSOR_ENCRYPTION_KEY=blank_key,
    )

    app = create_app(settings)

    assert settings.cursor_encryption_key is None
    assert app.state.cursor_codec is None


def settings_context(actor_id, *capabilities):
    return InternalAuthorizationContext(
        actor_id=actor_id,
        capabilities=frozenset(capabilities),
    )


def source_reference(key_value="0123456789abcdef01234567", *, key_field="_id"):
    return SourceReference(
        database="byd-leads-new",
        collection="users",
        key_field=key_field,
        key_type="objectid",
        key_value=key_value,
    )


def mapping_record(user_id, verified_by, **overrides):
    values = {
        "id": uuid4(),
        "user_id": user_id,
        "source_ref": source_reference(),
        "mapping_version": 1,
        "active": False,
        "sites": ("site_a",),
        "verified_by": verified_by,
        "verified_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return IdentityMapping(**(values | overrides))


def app_setting(setting_id, **overrides):
    values = {"id": setting_id, "created_at": NOW, "updated_at": NOW}
    return AppSetting(**(values | overrides))


def settings_audit(actor_id):
    return AppAuditEvent(
        actor_id=actor_id,
        action="access_control_change",
        outcome="success",
        request_id=uuid4(),
        created_at=NOW,
    )


async def current_policy(client):
    return client.database["app_settings"].documents["access_control"]["policy_version"]


async def seed_users(database, client, actor_id, *users):
    async with database.transaction(full_context(actor_id)) as transaction:
        await transaction.advance_access_control_fence(await current_policy(client))
        for user in users:
            await transaction.create_user(user)
        await transaction.append_required_audit(audit(actor_id, users[0].id))


async def persist_mapping(
    database,
    actor_id,
    record,
    *,
    policy_version,
    expected_version=None,
    append_audit=True,
):
    context = settings_context(
        actor_id,
        StorageCapability.READ_SETTINGS,
        StorageCapability.WRITE_SETTINGS,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    )
    async with database.transaction(context) as transaction:
        await transaction.advance_access_control_fence(policy_version)
        await transaction.save_mapping(record, expected_version=expected_version)
        if append_audit:
            await transaction.append_required_audit(settings_audit(actor_id))


async def persist_setting(
    database,
    actor_id,
    record,
    *,
    policy_version,
    expected_version=None,
    append_audit=True,
):
    context = settings_context(
        actor_id,
        StorageCapability.READ_SETTINGS,
        StorageCapability.WRITE_SETTINGS,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    )
    async with database.transaction(context) as transaction:
        await transaction.advance_access_control_fence(policy_version)
        await transaction.save_setting(record, expected_version=expected_version)
        if append_audit:
            await transaction.append_required_audit(settings_audit(actor_id))


@pytest.mark.asyncio
async def test_mapping_indexes_use_the_source_qualified_binding_tuple():
    database, client, _events = await connect_fake()

    await database.initialize_mapping_indexes()

    indexes = {
        options["name"]: (keys, options)
        for keys, options in client.database["identity_mappings"].indexes
    }
    assert indexes["uq_source_identity"][0] == (
        ("source_ref.database", 1),
        ("source_ref.collection", 1),
        ("source_ref.key_field", 1),
        ("source_ref.key_type", 1),
        ("source_ref.key_value", 1),
    )
    assert indexes["uq_source_identity"][1]["unique"] is True
    assert indexes["ix_user_active"][0] == (("user_id", 1), ("active", 1))
    assert indexes["ix_user_active"][1].get("unique") is not True
    assert client.selected_databases == ["byd_ai_layer"]


def test_mapping_requires_a_stable_source_reference_and_safe_defaults():
    actor_id = uuid4()
    record = mapping_record(uuid4(), actor_id)

    assert record.active is False
    assert record.mapping_version == 1
    assert record.source_ref.key_field == "_id"
    assert "verified_by" in record.document()
    assert "verified_at" in record.document()
    assert not {"name", "email", "password", "token"}.intersection(record.document())

    with pytest.raises(ValidationError):
        mapping_record(uuid4(), actor_id, source_ref=source_reference(key_field="name"))
    with pytest.raises(ValidationError):
        mapping_record(uuid4(), actor_id, sites=("All Sites",))
    with pytest.raises(ValidationError):
        mapping_record(uuid4(), actor_id, extra_source_name="Staff Name")


def test_site_and_retention_settings_have_safe_typed_defaults():
    sites = app_setting("sites")
    retention = app_setting("retention")
    disclosure = app_setting("disclosure")
    site = SiteConfiguration(
        site_id="site_a",
        aliases=("Doncaster",),
        timezone="Australia/Melbourne",
    )

    assert sites.sites == ()
    assert retention.saved_retention_days is None
    assert retention.audit_retention_days is None
    assert disclosure.disclosure_enabled is False
    assert site.timezone == "Australia/Melbourne"


def test_settings_reject_unsafe_ambiguous_or_unapproved_values():
    with pytest.raises(ValidationError, match="Unknown timezone"):
        SiteConfiguration(site_id="site_a", timezone="Mars/Olympus")
    for alias in ("All Sites", "*", "bad<alias", "line\nbreak"):
        with pytest.raises(ValidationError):
            SiteConfiguration(site_id="site_a", aliases=(alias,))
    for days in (0, 36501):
        with pytest.raises(ValidationError):
            app_setting("retention", saved_retention_days=days)
    ambiguous_sites = (
        SiteConfiguration(site_id="site_a", aliases=("Melbourne",)),
        SiteConfiguration(site_id="site_b", aliases=(" melbourne ",)),
    )
    with pytest.raises(ValidationError, match="Ambiguous"):
        app_setting("sites", sites=ambiguous_sites)
    site_id_collision = (
        SiteConfiguration(site_id="site_a"),
        SiteConfiguration(site_id="Doncaster", aliases=("SITE_A",)),
    )
    with pytest.raises(ValidationError, match="Ambiguous"):
        app_setting("sites", sites=site_id_collision)
    with pytest.raises(ValidationError):
        app_setting("sites", saved_retention_days=30)
    with pytest.raises(ValidationError):
        app_setting("retention", filter={"$where": "true"})
    with pytest.raises(ValidationError):
        app_setting("disclosure", disclosure_enabled=True)
    with pytest.raises(ValidationError):
        AppSetting(id="access_control", created_at=NOW, updated_at=NOW)


@pytest.mark.asyncio
async def test_settings_are_private_and_reject_unlisted_names_without_reads():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    context = settings_context(actor_id, StorageCapability.READ_SETTINGS)

    assert not hasattr(database, "get_setting")
    assert not hasattr(database, "save_setting")
    assert not hasattr(database, "get_mapping")
    assert not hasattr(database, "save_mapping")
    async with database.transaction(context) as transaction:
        with pytest.raises(ValueError, match="Unknown setting"):
            await transaction.get_setting("access_control")
        with pytest.raises(ValueError, match="Unknown setting"):
            await transaction.get_setting("arbitrary_collection")
        with pytest.raises(AuthorizationDenied):
            await transaction.save_setting(
                app_setting("retention", saved_retention_days=30)
            )
    assert client.database.collections.keys() == {"app_settings"}


@pytest.mark.asyncio
async def test_typed_setting_can_round_trip_with_settings_capability_only():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    record = app_setting(
        "sites",
        sites=(
            SiteConfiguration(
                site_id="site_a",
                aliases=("Doncaster showroom",),
                timezone="Australia/Melbourne",
            ),
        ),
    )
    assert StorageCapability.WRITE_IDENTITIES not in {
        StorageCapability.READ_SETTINGS,
        StorageCapability.WRITE_SETTINGS,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    }

    await persist_setting(
        database,
        actor_id,
        record,
        policy_version=POLICY_VERSION,
    )

    context = settings_context(actor_id, StorageCapability.READ_SETTINGS)
    async with database.transaction(context) as transaction:
        loaded = await transaction.get_setting("sites")
    assert loaded == record
    stored = client.database["app_settings"].documents["sites"]
    assert stored["sites"][0]["site_id"] == "site_a"
    assert stored["sites"][0]["timezone"] == "Australia/Melbourne"
    assert set(client.database.collections) == {"app_settings", "audit_events"}


@pytest.mark.asyncio
async def test_settings_mutations_require_fence_policy_and_audit():
    database, client, events = await connect_fake()
    actor_id = uuid4()
    record = app_setting("retention", saved_retention_days=30)
    context = settings_context(
        actor_id,
        StorageCapability.READ_SETTINGS,
        StorageCapability.WRITE_SETTINGS,
    )

    with pytest.raises(ValueError, match="fence"):
        async with database.transaction(context) as transaction:
            await transaction.save_setting(record)
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]
    assert "retention" not in client.database["app_settings"].documents

    with pytest.raises(AppWriteConflict, match="policy changed"):
        await persist_setting(
            database,
            actor_id,
            record,
            policy_version=POLICY_VERSION - 1,
        )
    assert "retention" not in client.database["app_settings"].documents

    with pytest.raises(RequiredAuditMissing):
        await persist_setting(
            database,
            actor_id,
            record,
            policy_version=POLICY_VERSION,
            append_audit=False,
        )
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_failed_required_audit_aborts_settings_write():
    database, client, events = await connect_fake()
    actor_id = uuid4()
    client.database["audit_events"].fail_insert = True

    with pytest.raises(AppDatabaseUnavailable, match="Required audit write failed"):
        await persist_setting(
            database,
            actor_id,
            app_setting("retention", saved_retention_days=30),
            policy_version=POLICY_VERSION,
        )
    assert events[-3:] == ["transaction-start", "transaction-abort", "session-close"]


@pytest.mark.asyncio
async def test_setting_updates_use_record_version_compare_and_swap():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    original = app_setting("retention", saved_retention_days=30)
    await persist_setting(
        database,
        actor_id,
        original,
        policy_version=POLICY_VERSION,
    )
    policy_version = await current_policy(client)
    changed = AppSetting(
        **(
            original.model_dump()
            | {
                "saved_retention_days": 60,
                "version": 1,
                "updated_at": NOW + timedelta(minutes=1),
            }
        )
    )
    await persist_setting(
        database,
        actor_id,
        changed,
        policy_version=policy_version,
        expected_version=0,
    )

    assert (
        client.database["app_settings"].documents["retention"]["saved_retention_days"]
        == 60
    )
    stale = AppSetting(
        **(
            changed.model_dump()
            | {
                "saved_retention_days": 90,
                "updated_at": NOW + timedelta(minutes=2),
            }
        )
    )
    with pytest.raises(AppWriteConflict, match="version changed"):
        await persist_setting(
            database,
            actor_id,
            stale,
            policy_version=await current_policy(client),
            expected_version=0,
        )
    assert (
        client.database["app_settings"].documents["retention"]["saved_retention_days"]
        == 60
    )


@pytest.mark.asyncio
async def test_mapping_requires_existing_user_and_verifying_actor():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    parent = user_record()

    with pytest.raises(ValueError):
        await persist_mapping(
            database,
            actor_id,
            mapping_record(parent.id, actor_id),
            policy_version=await current_policy(client),
        )
    await seed_users(database, client, actor_id, parent)
    with pytest.raises(ValueError, match="Verified service mapping"):
        await persist_mapping(
            database,
            actor_id,
            mapping_record(parent.id, uuid4()),
            policy_version=await current_policy(client),
        )
    assert not client.database["identity_mappings"].documents


@pytest.mark.asyncio
async def test_duplicate_source_mapping_returns_safe_conflict():
    database, client, _events = await connect_fake()
    await database.initialize_mapping_indexes()
    actor_id = uuid4()
    first_user, second_user = user_record(), user_record()
    await seed_users(database, client, actor_id, first_user, second_user)
    first = mapping_record(first_user.id, actor_id)
    await persist_mapping(
        database,
        actor_id,
        first,
        policy_version=await current_policy(client),
    )
    duplicate = mapping_record(second_user.id, actor_id)

    with pytest.raises(AppWriteConflict, match="already exists"):
        await persist_mapping(
            database,
            actor_id,
            duplicate,
            policy_version=await current_policy(client),
        )
    assert len(client.database["identity_mappings"].documents) == 1
    assert str(duplicate.id) not in client.database["identity_mappings"].documents


@pytest.mark.asyncio
async def test_mapping_update_requires_stable_binding_and_both_versions():
    database, client, _events = await connect_fake()
    await database.initialize_mapping_indexes()
    actor_id = uuid4()
    first_user, second_user = user_record(), user_record()
    await seed_users(database, client, actor_id, first_user, second_user)
    original = mapping_record(first_user.id, actor_id)
    await persist_mapping(
        database,
        actor_id,
        original,
        policy_version=await current_policy(client),
    )
    version2 = IdentityMapping(
        **(
            original.model_dump()
            | {
                "sites": ("site_b",),
                "active": True,
                "mapping_version": 2,
                "version": 1,
                "verified_at": NOW + timedelta(minutes=1),
                "updated_at": NOW + timedelta(minutes=1),
            }
        )
    )
    await persist_mapping(
        database,
        actor_id,
        version2,
        policy_version=await current_policy(client),
        expected_version=0,
    )

    stored = client.database["identity_mappings"].documents[str(original.id)]
    assert stored["version"] == 1
    assert stored["mapping_version"] == 2
    assert stored["active"] is True
    stale = IdentityMapping(
        **(
            version2.model_dump()
            | {
                "sites": ("site_c",),
                "mapping_version": 3,
                "version": 1,
                "verified_at": NOW + timedelta(minutes=2),
                "updated_at": NOW + timedelta(minutes=2),
            }
        )
    )
    with pytest.raises(AppWriteConflict, match="version changed"):
        await persist_mapping(
            database,
            actor_id,
            stale,
            policy_version=await current_policy(client),
            expected_version=0,
        )

    changed_binding = IdentityMapping(
        **(
            version2.model_dump()
            | {
                "user_id": second_user.id,
                "mapping_version": 3,
                "version": 2,
                "verified_at": NOW + timedelta(minutes=2),
                "updated_at": NOW + timedelta(minutes=2),
            }
        )
    )
    with pytest.raises(AppWriteConflict, match="binding or version changed"):
        await persist_mapping(
            database,
            actor_id,
            changed_binding,
            policy_version=await current_policy(client),
            expected_version=1,
        )


@pytest.mark.asyncio
async def test_mapping_parent_lookup_and_write_need_only_settings_capability():
    database, client, _events = await connect_fake()
    actor_id = uuid4()
    parent = user_record()
    await seed_users(database, client, actor_id, parent)
    await database.initialize_mapping_indexes()
    record = mapping_record(parent.id, actor_id)

    await persist_mapping(
        database,
        actor_id,
        record,
        policy_version=await current_policy(client),
    )
    context = settings_context(actor_id, StorageCapability.READ_SETTINGS)
    async with database.transaction(context) as transaction:
        loaded = await transaction.get_mapping(record.id)
    assert loaded == record
    assert (
        client.database["identity_mappings"].documents[str(record.id)]["active"]
        is False
    )
