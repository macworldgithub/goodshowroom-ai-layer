"""TICKET-016 cross-boundary temporary privacy and failure precedence checks."""

import json
import logging
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest
from agents.tracing import custom_span

from goodshowroom_ai.agent_service import QuestionPlan
from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.responses import BuiltResponse, ResponseContext
from goodshowroom_ai.storage.chat_turns import TurnCommand
from goodshowroom_ai.temporary import (
    LogoutCoordinator,
    TemporaryMemoryStore,
    TemporaryUnavailable,
)
from goodshowroom_ai.trace_export import sanitize_payload
from goodshowroom_ai.tracing import configure_tracing
from tests.test_agent_service import ScriptedRunner, _setup
from tests.test_chat_turns import (
    TurnFixture,
    commit_attempt,
    only_record,
    start_attempt,
)
from tests.test_operational_reads import NOW
from tests.test_request_tracing import _prepare
from tests.test_responses import complete_response
from tests.test_trace_export import make_native, serialized_requests
from tests.test_tracing import (
    SYNTHETIC_SECRET,
    FailingProcessor,
    MemoryProcessor,
    settings,
)

SECRET_SENTINEL = "SYNTHETIC-TEMPORARY-SECRET-SENTINEL-016-7f3c"


def _temporary_service():
    """Wire the real in-memory temporary store into the agent service."""
    fixture, service, repository, responses, _tracing, principal, _, _ = _setup(
        mode="temporary"
    )
    store = TemporaryMemoryStore(
        authorization=fixture["authorization"], clock=lambda: NOW
    )
    service._memory = store
    return service, repository, responses, principal, store


async def _new_temporary_turn(store, principal, message):
    chat = await store.create(principal, uuid4())
    request_id = uuid4()
    context = ResponseContext(
        request_id=request_id,
        session_id=chat.id,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    command = TurnCommand(request_id=request_id, expected_version=0, message=message)
    return context, command


@pytest.mark.asyncio
async def test_temporary_secret_is_absent_from_traces_logs_and_saved_store(caplog):
    service, repository, responses, principal, store = _temporary_service()
    context, command = await _new_temporary_turn(store, principal, SECRET_SENTINEL)
    service._runner = ScriptedRunner(
        QuestionPlan(intent="count_leads", site="site_a"),
        tool_arguments={"site": "site_a"},
    )
    capture = MemoryProcessor()
    runtime = configure_tracing(
        settings(app_env="development", enabled=True), capture_processor=capture
    )
    service._tracing = runtime
    try:
        with caplog.at_level(logging.DEBUG):
            outcome = await service.run_turn(principal, context, command)
        assert outcome.status == "complete"
        assert runtime.export_enabled is False
        assert len(capture.trace_ends) == 1
        root = capture.trace_ends[0].export()
        assert root["workflow_name"] == "GoodShowroom.TemporaryChat"
        assert capture.span_ends
        exports = [
            sanitize_payload(root),
            *(sanitize_payload(span.export()) for span in capture.span_ends),
        ]
        assert SECRET_SENTINEL not in json.dumps(exports)
        assert SECRET_SENTINEL not in caplog.text
        # The sentinel really entered the temporary turn; absence is not vacuous.
        entry = store._entries[context.session_id]
        assert [turn.user_message for turn in entry.turns] == [SECRET_SENTINEL]
        assert responses.metric_calls[0][2].site == "site_a"
    finally:
        runtime.close()
    assert repository.begin_calls == []
    assert repository.load_calls == []
    assert repository.commit_calls == []
    assert repository.fail_calls == []


@pytest.mark.asyncio
async def test_processor_failure_does_not_override_real_temporary_denial(caplog):
    service, repository, responses, principal, context, command = _prepare("temporary")
    service._runner = ScriptedRunner(
        QuestionPlan(intent="count_leads", site="site_b"),
        tool_arguments={"site": "site_b"},
    )
    runtime = configure_tracing(
        settings(app_env="development", enabled=True),
        capture_processor=FailingProcessor(),
    )
    service._tracing = runtime
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.tracing"):
            with pytest.raises(AccessDenied) as denied:
                await service.run_turn(principal, context, command)
        assert "site_b" not in str(denied.value)
        assert "tracing_diagnostic_failure" in caplog.text
        assert SYNTHETIC_SECRET not in caplog.text
    finally:
        runtime.close()
    assert responses.metric_calls == []
    assert service._memory.commits == []
    assert repository.begin_calls == repository.load_calls == []
    assert repository.commit_calls == repository.fail_calls == []


@pytest.mark.asyncio
async def test_logout_after_result_blocks_late_replay_without_saved_persistence():
    service, repository, _responses, principal, store = _temporary_service()
    context, command = await _new_temporary_turn(
        store, principal, "Synthetic late temporary question"
    )
    service._runner = ScriptedRunner(
        QuestionPlan(intent="count_leads", site="site_a"),
        tool_arguments={"site": "site_a"},
    )
    outcome = await service.run_turn(principal, context, command)
    assert outcome.status == "complete"

    revoked = []

    async def fail_revoke(_principal):
        revoked.append(True)
        raise RuntimeError("synthetic backend revocation failure")

    result = await LogoutCoordinator(memory=store, revoke_login=fail_revoke).logout(
        principal
    )
    assert result.backend_invalidated is False
    assert result.cleanup_confirmed is False
    assert revoked == [True]
    assert store._entries == {}
    with pytest.raises(TemporaryUnavailable):
        await service.run_turn(principal, context, command)
    assert repository.begin_calls == []
    assert repository.commit_calls == []


@pytest.mark.asyncio
async def test_saved_commit_and_replay_survive_native_export_failure(caplog):
    """A dropped trace must not duplicate or hide the one authoritative turn."""
    fixture = TurnFixture()
    await fixture.create_parent()
    sentinel = "SYNTHETIC-SAVED-TURN-SENTINEL-016"
    command = fixture.command(message=sentinel)
    started = await start_attempt(fixture, command)

    def fail_request(request):
        raise httpx2.ConnectError(SYNTHETIC_SECRET, request=request)

    requests, transport = serialized_requests(fail_request)
    native = make_native(transport)
    runtime = configure_tracing(
        settings(app_env="development", enabled=True), native_exporter=native
    )
    try:
        with caplog.at_level(logging.WARNING):
            with runtime.root_trace("saved"):
                with custom_span("authorization", data={"outcome": "success"}):
                    pass
                committed = await commit_attempt(fixture, command, started)
            runtime.close()
    finally:
        runtime.close()

    assert committed.status == "committed"
    assert len(requests) == 1
    assert sentinel not in caplog.text
    assert sentinel not in requests[0].read().decode("utf-8")
    turn = only_record(fixture.turns)
    assert turn["user_message"] == sentinel
    assert len(fixture.audits.documents) == 1

    turns_before = deepcopy(fixture.turns.documents)
    audits_before = deepcopy(fixture.audits.documents)
    writes_before = sum(
        len(collection.writes) for collection in fixture.database.collections.values()
    )
    replay = await fixture.repository.commit_completed_turn(
        fixture.decision(),
        fixture.parent.id,
        command,
        fixture.built_response(command),
        started.run_id,
    )

    assert replay.status == "committed"
    assert replay.replayed is True
    assert replay.turn == committed.turn
    assert fixture.turns.documents == turns_before
    assert fixture.audits.documents == audits_before
    assert (
        sum(
            len(collection.writes)
            for collection in fixture.database.collections.values()
        )
        == writes_before
    )


@pytest.mark.asyncio
async def test_trace_processor_failure_does_not_mask_saved_policy_revocation(caplog):
    """Saved release still denies when grants change after response construction."""
    (
        fixture,
        service,
        repository,
        responses,
        _tracing,
        principal,
        context,
        command,
    ) = _setup()

    def revoke_q01_grants():
        snapshot = fixture["state"]["snapshot"]
        role = snapshot.role.model_copy(
            update={
                "grants": tuple(
                    grant.model_copy(update={"tools": ()})
                    for grant in snapshot.role.grants
                )
            }
        )
        user = snapshot.user.model_copy(
            update={
                "scope_limit": tuple(
                    grant.model_copy(update={"tools": ()})
                    for grant in snapshot.user.scope_limit
                )
            }
        )
        fixture["state"]["snapshot"] = snapshot.model_copy(
            update={"role": role, "user": user}
        )

    responses.after_metric = revoke_q01_grants
    service._runner = ScriptedRunner(
        QuestionPlan(intent="count_leads", site="site_a"),
        tool_arguments={"site": "site_a"},
    )
    runtime = configure_tracing(
        settings(app_env="development", enabled=True),
        capture_processor=FailingProcessor(),
    )
    service._tracing = runtime
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.tracing"):
            with pytest.raises(AccessDenied) as denied:
                await service.run_turn(principal, context, command)
        assert "site_a" not in str(denied.value)
        assert "tracing_diagnostic_failure" in caplog.text
        assert SYNTHETIC_SECRET not in caplog.text
    finally:
        runtime.close()

    assert len(responses.metric_calls) == 1
    assert repository.commit_calls == []
    assert repository.fail_calls[0][4] == "authorization_changed"


@pytest.mark.asyncio
async def test_temporary_lease_expiry_and_restart_deny_reuse_without_saved_fallback():
    """Expired memory leases and process restart never fall back to saved storage."""
    fixture, _service, repository, _responses, _tracing, principal, _, _ = _setup(
        mode="temporary"
    )
    clock_value = NOW

    def clock():
        return clock_value

    store = TemporaryMemoryStore(authorization=fixture["authorization"], clock=clock)
    sentinel = "SYNTHETIC-TEMPORARY-LEASE-SENTINEL-016"
    chat = await store.create(principal, uuid4())
    request_id = uuid4()
    context = ResponseContext(
        request_id=request_id,
        session_id=chat.id,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )
    command = TurnCommand(request_id=request_id, expected_version=0, message=sentinel)

    assert (await store.begin_turn(principal, context, command)).status == "started"
    # Attempt state is content-free while the synthetic message is only in memory.
    assert sentinel not in repr(store._entries)

    clock_value = NOW + timedelta(minutes=4)
    with pytest.raises(AccessDenied):
        await store.commit_completed_turn(
            principal,
            context,
            command,
            BuiltResponse(request_id=request_id, response=complete_response()),
        )
    # The periodic sweep removes the expired in-memory transcript.
    store.sweep()
    assert store._entries == {}

    restarted = TemporaryMemoryStore(
        authorization=fixture["authorization"], clock=lambda: NOW
    )
    try:
        with pytest.raises(TemporaryUnavailable):
            await restarted.begin_turn(principal, context, command)
    finally:
        await restarted.close()

    assert repository.begin_calls == []
    assert repository.load_calls == []
    assert repository.commit_calls == []
    assert repository.fail_calls == []
