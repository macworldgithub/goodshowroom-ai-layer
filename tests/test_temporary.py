"""TICKET-010 temporary context lifecycle and logout boundaries."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.authorization import AuthorizationService, Principal
from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.responses import BuiltResponse, ResponseContext
from goodshowroom_ai.storage.chat_turns import TurnCommand
from goodshowroom_ai.temporary import (
    LogoutCoordinator,
    TemporaryMemoryStore,
    TemporaryUnavailable,
)
from tests.test_authorization import NOW, policy_fixture, service_for
from tests.test_responses import complete_response


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def setup_store(*, max_chats=256):
    fixture = policy_fixture()
    clock = Clock()
    authorization = service_for(fixture, clock=clock)
    store = TemporaryMemoryStore(
        authorization=authorization, clock=clock, max_chats=max_chats
    )
    return fixture["principal"], store, clock, authorization, fixture


async def create_context(store, principal, *, request_id=None, message="Question"):
    request_id = request_id or uuid4()
    chat = await store.create(principal, request_id)
    context = ResponseContext(
        request_id=uuid4(),
        session_id=chat.id,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    command = TurnCommand(
        request_id=context.request_id, expected_version=0, message=message
    )
    return chat, context, command


def built(command):
    response = complete_response().model_copy(update={"policy_version": 17})
    return BuiltResponse(request_id=command.request_id, response=response)


@pytest.mark.asyncio
async def test_temporary_turn_replay_precedes_version_check_and_changed_payload_conflicts(
    caplog,
):
    principal, store, _clock, _authorization, _fixture = setup_store()
    creation_id = uuid4()
    sentinel = "TEMPORARY-CONTENT-SENTINEL-010"
    chat, context, command = await create_context(
        store, principal, request_id=creation_id, message=sentinel
    )
    assert (await store.create(principal, creation_id)).id == chat.id

    assert (await store.begin_turn(principal, context, command)).status == "started"
    history = await store.load(principal, context, command)
    assert history.mode == "temporary"
    assert history.turns == ()
    await store.commit_completed_turn(principal, context, command, built(command))
    await store.finish_turn(principal, context, command)

    replay = await store.begin_turn(principal, context, command)
    assert replay.status == "committed"
    assert replay.built == built(command)
    await store.validate_release(principal, context, command)
    assert (await store.begin_turn(principal, context, command)).status == "committed"
    changed = command.model_copy(update={"message": "Changed payload"})
    with pytest.raises(ApiProblem):
        await store.begin_turn(principal, context, changed)
    assert sentinel not in caplog.text
    assert not hasattr(store, "insert_many")


@pytest.mark.asyncio
async def test_evicted_reply_identity_blocks_reuse_and_unknown_pending_id_can_retry():
    principal, store, _clock, _authorization, _fixture = setup_store()
    chat = await store.create(principal, uuid4())
    first_context = None
    first_command = None
    latest_context = None
    latest_command = None
    for version in range(33):
        request_id = uuid4()
        context = ResponseContext(
            request_id=request_id,
            session_id=chat.id,
            owner_id=principal.user_id,
            mode="temporary",
            chat_login_id=principal.login_id,
        )
        command = TurnCommand(
            request_id=request_id,
            expected_version=version,
            message=f"Synthetic notice {version}",
        )
        assert (await store.begin_turn(principal, context, command)).status == "started"
        await store.commit_completed_turn(principal, context, command, built(command))
        await store.finish_turn(principal, context, command)
        if version == 0:
            first_context, first_command = context, command
        if version == 32:
            latest_context, latest_command = context, command

    assert first_context is not None and first_command is not None
    assert latest_context is not None and latest_command is not None
    changed_old_request = first_command.model_copy(
        update={"expected_version": 33, "message": "Reused request id"}
    )
    changed_context = first_context.model_copy(
        update={"request_id": changed_old_request.request_id}
    )
    with pytest.raises(ApiProblem) as conflict:
        await store.begin_turn(principal, changed_context, changed_old_request)
    assert conflict.value.code.value == "idempotency_conflict"
    assert (
        await store.request_status(principal, first_context, first_command) == "unknown"
    )
    assert (
        await store.begin_turn(principal, latest_context, latest_command)
    ).status == "committed"

    pending_id = uuid4()
    pending_context = ResponseContext(
        request_id=pending_id,
        session_id=chat.id,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    pending_command = TurnCommand(
        request_id=pending_id, expected_version=33, message="Pending synthetic turn"
    )
    await store.begin_turn(principal, pending_context, pending_command)
    unknown_id = uuid4()
    unknown_context = ResponseContext(
        request_id=unknown_id,
        session_id=chat.id,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    unknown_command = TurnCommand(
        request_id=unknown_id, expected_version=33, message="Unknown synthetic turn"
    )
    assert (
        await store.request_status(principal, unknown_context, unknown_command)
        == "retry_allowed"
    )
    await store.finish_turn(principal, pending_context, pending_command)


@pytest.mark.asyncio
async def test_idle_expiry_and_end_prevent_reuse_with_one_unavailable_error():
    principal, store, clock, authorization, fixture = setup_store()
    chat, context, command = await create_context(store, principal)
    await store.end(principal, chat.id)
    with pytest.raises(TemporaryUnavailable) as ended:
        await store.begin_turn(principal, context, command)

    replacement, expired_context, expired_command = await create_context(
        store, principal
    )
    # Verified login refreshes extend only the five-minute lease. They do not
    # extend the temporary chat's original idle deadline.
    for _ in range(9):
        clock.value += timedelta(minutes=3)
        login = fixture["snapshot"].login.model_copy(
            update={
                "updated_at": clock.value,
                "lease_valid_until": clock.value + timedelta(minutes=5),
            }
        )
        fixture["snapshot"] = fixture["snapshot"].model_copy(update={"login": login})
        await store.refresh_login(principal)
    clock.value += timedelta(minutes=3)
    store.sweep()
    with pytest.raises(TemporaryUnavailable) as expired:
        await store.begin_turn(principal, expired_context, expired_command)
    assert str(ended.value) == str(expired.value)
    await store.end(principal, replacement.id)


@pytest.mark.asyncio
async def test_expired_five_minute_lease_cannot_revive_context_after_fresh_login():
    principal, store, clock, _authorization, fixture = setup_store()
    login = fixture["snapshot"].login.model_copy(
        update={
            "updated_at": NOW,
            "lease_valid_until": NOW + timedelta(minutes=5),
        }
    )
    fixture["snapshot"] = fixture["snapshot"].model_copy(update={"login": login})
    _chat, context, command = await create_context(store, principal)

    clock.value += timedelta(minutes=5)
    # A later verified login can refresh the account lease, but sweep removes
    # the chat against its earlier cached lease before renewal is applied.
    login = fixture["snapshot"].login.model_copy(
        update={
            "updated_at": clock.value,
            "lease_valid_until": clock.value + timedelta(minutes=5),
        }
    )
    fixture["snapshot"] = fixture["snapshot"].model_copy(update={"login": login})
    await store.refresh_login(principal)
    clock.value += timedelta(seconds=60)
    store.sweep()
    with pytest.raises(TemporaryUnavailable):
        await store.begin_turn(principal, context, command)


@pytest.mark.asyncio
async def test_restart_and_capacity_never_create_durable_recovery():
    principal, store, clock, authorization, _fixture = setup_store(max_chats=1)
    first, context, command = await create_context(store, principal)
    with pytest.raises(TemporaryUnavailable):
        await create_context(store, principal)

    restarted = TemporaryMemoryStore(
        authorization=authorization,
        clock=clock,
        max_chats=1,
    )
    with pytest.raises(TemporaryUnavailable):
        await restarted.begin_turn(principal, context, command)
    await store.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_app_lifecycle_starts_and_closes_only_injected_temporary_memory():
    principal, store, _clock, _authorization, _fixture = setup_store()
    app = create_app(Settings(_env_file=None, app_env="test"), temporary_memory=store)
    assert app.state.temporary_memory is store
    async with app.router.lifespan_context(app):
        assert store._sweeper is not None
    with pytest.raises(TemporaryUnavailable):
        await store.create(principal, uuid4())

    default_app = create_app(Settings(_env_file=None, app_env="test"))
    assert default_app.state.temporary_memory is None


@pytest.mark.asyncio
async def test_valid_other_user_or_login_cannot_distinguish_unknown_context_ids():
    owner_fixture = policy_fixture()
    other_fixture = policy_fixture()
    clock = Clock()
    owner = owner_fixture["principal"]
    other = other_fixture["principal"]
    alternate_login_id = uuid4()
    alternate_generation = owner.generation + 1
    alternate = Principal(
        user_id=owner.user_id,
        login_id=alternate_login_id,
        generation=alternate_generation,
    )
    alternate_login = owner_fixture["snapshot"].login.model_copy(
        update={"id": alternate_login_id, "generation": alternate_generation}
    )
    alternate_snapshot = owner_fixture["snapshot"].model_copy(
        update={"login": alternate_login}
    )
    snapshots = {
        (owner.user_id, owner.login_id, owner.generation): owner_fixture["snapshot"],
        (other.user_id, other.login_id, other.generation): other_fixture["snapshot"],
        (
            alternate.user_id,
            alternate.login_id,
            alternate.generation,
        ): alternate_snapshot,
    }

    async def load_policy(principal):
        return snapshots.get(
            (principal.user_id, principal.login_id, principal.generation)
        )

    async def check_identity(principal):
        return (
            principal.user_id,
            principal.login_id,
            principal.generation,
        ) in snapshots

    authorization = AuthorizationService(
        load_policy=load_policy,
        check_identity=check_identity,
        clock=clock,
    )
    store = TemporaryMemoryStore(authorization=authorization, clock=clock)
    _chat, context, command = await create_context(store, owner)
    outcomes = []
    for principal in (other, alternate):
        with pytest.raises(TemporaryUnavailable) as denied:
            await store.begin_turn(principal, context, command)
        outcomes.append(denied.value.code)
    unknown_context = context.model_copy(update={"session_id": uuid4()})
    with pytest.raises(TemporaryUnavailable) as unknown:
        await store.begin_turn(owner, unknown_context, command)
    assert outcomes == [unknown.value.code, unknown.value.code]
    await store.close()


@pytest.mark.asyncio
async def test_logout_invalidates_local_context_before_failed_backend_revoke():
    principal, store, _clock, _authorization, _fixture = setup_store()
    _chat, context, command = await create_context(store, principal)
    revoked = []

    async def fail_revoke(_principal):
        revoked.append(True)
        raise RuntimeError("secret backend detail")

    result = await LogoutCoordinator(memory=store, revoke_login=fail_revoke).logout(
        principal
    )
    assert result.backend_invalidated is False
    assert result.cleanup_confirmed is False
    assert revoked == [True]
    with pytest.raises(TemporaryUnavailable):
        await store.begin_turn(principal, context, command)


@pytest.mark.asyncio
async def test_login_invalidation_cancels_active_work_and_cannot_resurrect_late_commit():
    principal, store, _clock, _authorization, _fixture = setup_store()
    _chat, context, command = await create_context(store, principal)
    entered = asyncio.Event()

    async def active_turn():
        await store.begin_turn(principal, context, command)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(active_turn())
    await entered.wait()
    await store.invalidate_login(principal)
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(TemporaryUnavailable):
        await store.commit_completed_turn(principal, context, command, built(command))
