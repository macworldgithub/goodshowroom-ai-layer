"""Offline integration checks for request-stage trace privacy and isolation."""

import json

import pytest

from goodshowroom_ai.agent_context import AgentHistory
from goodshowroom_ai.agent_service import QuestionPlan
from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.operational import SourceUnavailable
from goodshowroom_ai.responses import ResponseInvalid
from goodshowroom_ai.trace_export import sanitize_payload
from goodshowroom_ai.tracing import configure_tracing
from tests.test_agent_service import MemoryStub, ScriptedRunner, _setup
from tests.test_tracing import FailingProcessor, MemoryProcessor, settings


def _prepare(mode):
    _, service, repository, responses, _, principal, context, command = _setup(
        mode=mode
    )
    if mode == "temporary":
        service._memory = MemoryStub(
            AgentHistory(
                owner_id=context.owner_id,
                session_id=context.session_id,
                mode=mode,
                login_id=principal.login_id,
                version=command.expected_version,
                policy_version=7,
            )
        )
    return service, repository, responses, principal, context, command


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["saved", "temporary"])
async def test_full_request_stages_have_one_root_and_safe_metadata(mode):
    service, repository, _, principal, context, command = _prepare(mode)
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
        outcome = await service.run_turn(principal, context, command)
        assert outcome.status == ("committed" if mode == "saved" else "complete")
        assert len(capture.trace_ends) == 1
        root = capture.trace_ends[0].export()
        assert root["workflow_name"] == (
            "GoodShowroom.SavedChat"
            if mode == "saved"
            else "GoodShowroom.TemporaryChat"
        )
        spans = [span.export() for span in capture.span_ends]
        stages = {
            span["span_data"]["name"]
            for span in spans
            if span["span_data"]["type"] == "custom"
        }
        assert {
            "authorization",
            "context_preparation",
            "argument_validation",
            "tool_execution",
            "response_construction",
            "final_policy_check",
        } <= stages
        assert ("persistence" if mode == "saved" else "memory") in stages
        assert all(span["trace_id"] == root["id"] for span in spans)
        ids = {span["id"] for span in spans}
        assert all(
            span["parent_id"] is None or span["parent_id"] in ids for span in spans
        )
        safe = [sanitize_payload(root), *(sanitize_payload(span) for span in spans)]
        payload = json.dumps(safe)
        assert command.message not in payload
        assert str(context.owner_id) not in payload
        assert str(context.session_id) not in payload
        assert str(context.request_id) not in payload
        if mode == "saved":
            assert len(repository.commit_calls) == 1
        else:
            assert len(service._memory.commits) == 1
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_source_failure_is_reported_without_record_content():
    service, repository, responses, principal, context, command = _prepare("saved")
    sentinel = "synthetic-source-record@example.test"

    async def fail(*_args):
        raise SourceUnavailable(sentinel)

    responses.build_metric = fail
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
        with pytest.raises(SourceUnavailable):
            await service.run_turn(principal, context, command)
        assert repository.commit_calls == []
        spans = [sanitize_payload(span.export()) for span in capture.span_ends]
        assert any(
            item["span_data"].get("data", {}).get("outcome") == "failed"
            for item in spans
        )
        assert sentinel not in json.dumps(spans)
    finally:
        runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["saved", "temporary"])
async def test_diagnostic_processor_failure_does_not_change_commit(mode):
    service, repository, _, principal, context, command = _prepare(mode)
    service._runner = ScriptedRunner(QuestionPlan(intent="unsupported"))
    runtime = configure_tracing(
        settings(app_env="development", enabled=True),
        capture_processor=FailingProcessor(),
    )
    service._tracing = runtime
    try:
        outcome = await service.run_turn(principal, context, command)
        assert outcome.status == ("committed" if mode == "saved" else "complete")
        commits = (
            repository.commit_calls if mode == "saved" else service._memory.commits
        )
        assert len(commits) == 1
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_diagnostic_processor_failure_does_not_override_denial():
    service, repository, _, principal, context, command = _prepare("saved")

    async def deny(*_args):
        raise AccessDenied("Access is not allowed.")

    service._authorization.authorize = deny
    runtime = configure_tracing(
        settings(app_env="development", enabled=True),
        capture_processor=FailingProcessor(),
    )
    service._tracing = runtime
    try:
        with pytest.raises(AccessDenied):
            await service.run_turn(principal, context, command)
        assert repository.begin_calls == []
        assert repository.commit_calls == []
    finally:
        runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "stage", "exception"),
    [
        ("model", "response_construction", RuntimeError),
        ("persistence", "persistence", RuntimeError),
        ("arguments", "argument_validation", ResponseInvalid),
    ],
)
async def test_failed_stages_exclude_raw_error_content(failure, stage, exception):
    service, repository, _, principal, context, command = _prepare("saved")
    sentinel = "synthetic-private-error@example.test"

    async def fail(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    if failure == "model":
        service._runner = fail
    elif failure == "persistence":
        service._runner = ScriptedRunner(QuestionPlan(intent="unsupported"))
        repository.commit_completed_turn = fail
    else:
        service._runner = ScriptedRunner(
            QuestionPlan(intent="count_leads", site="site_a"),
            raw_tool_arguments=json.dumps({"site": "site_a", "private": sentinel}),
        )

    capture = MemoryProcessor()
    runtime = configure_tracing(
        settings(app_env="development", enabled=True), capture_processor=capture
    )
    service._tracing = runtime
    try:
        with pytest.raises(exception):
            await service.run_turn(principal, context, command)
        spans = [sanitize_payload(span.export()) for span in capture.span_ends]
        assert any(
            span["span_data"].get("name") == stage
            and span["span_data"].get("data", {}).get("outcome") == "failed"
            for span in spans
        )
        assert sentinel not in json.dumps(spans)
        assert repository.commit_calls == []
    finally:
        runtime.close()
