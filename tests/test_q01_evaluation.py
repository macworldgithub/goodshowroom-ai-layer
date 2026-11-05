"""Offline Q-01 evaluation. Scripted plans are not live-model scores."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from goodshowroom_ai.agent_service import QuestionPlan
from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.metrics import MetricRequest
from goodshowroom_ai.storage.chat_sessions import ChatQuestionState
from tests.q01_evaluation import CASES, score
from tests.test_agent_service import ScriptedRunner, _service_with_runner, _setup
from tests.test_disclosure import disclosure_for, publication
from tests.test_metrics import (
    FROZEN,
    PERIOD_END,
    PERIOD_START,
    SITE_B_VALUE,
    document,
    metric_request,
)
from tests.test_responses import builder_for, context_for, metric_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_scripted_q01_corpus(case):
    state = None
    if case.intent == "follow_up":
        state = ChatQuestionState(
            metric_version=1,
            site="site_a",
            start_utc=datetime(2026, 10, 15, tzinfo=UTC),
            end_utc=datetime(2026, 10, 22, tzinfo=UTC),
            timezone=case.timezone,
            policy_version=7,
            through_sequence=1,
        )
    _, service, _, responses, _, principal, context, command = _setup(
        history_turns=case.prefix, state=state, version=len(case.prefix)
    )
    command = command.model_copy(update={"message": case.question})
    runner = ScriptedRunner(
        QuestionPlan(
            intent=case.intent, site="site_a" if case.intent == "count_leads" else None
        ),
        tool_arguments=dict(case.arguments) if case.arguments else None,
    )
    _service_with_runner(service, runner)
    outcome = await service.run_turn(principal, context, command)
    assert outcome.built is not None
    tools = (runner.calls[0][0].tools[0].name,) if case.arguments else ()
    score(
        case,
        tools=tools,
        arguments=runner.tool_arguments or {},
        response=outcome.built.response,
    )
    assert [call[2] for call in responses.metric_calls] == (
        [MetricRequest(site="site_a")] if case.arguments else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES[:5], ids=lambda case: case.id)
async def test_real_synthetic_count_and_visuals(case):
    rows = [document(n, "virtualyard", FROZEN - timedelta(days=5)) for n in range(1, 4)]
    fixture, config, collection, metrics = metric_fixture(rows)
    principal = fixture["principal"]
    evidence = await metrics.evaluate(principal, metric_request())
    assert evidence.value == 3
    disclosure = disclosure_for(
        fixture, config, metrics, publications=(publication(evidence),)
    )
    response = (
        await builder_for(fixture, config, disclosure).build_metric(
            principal, context_for(principal), metric_request()
        )
    ).response
    score(
        case, tools=("count_leads",), arguments=dict(case.arguments), response=response
    )
    assert response.scope.site == "site_a"
    assert not collection.write_calls


@pytest.mark.asyncio
async def test_partial_evidence_is_not_an_exact_total():
    rows = [
        document(1, "virtualyard", PERIOD_START),
        document(2, "dealer_studio", PERIOD_END - timedelta(microseconds=1)),
        document(3, "virtualyard", PERIOD_END),
        document(4, "virtualyard", None),
        document(5, "virtualyard", PERIOD_START, site=SITE_B_VALUE),
    ]
    fixture, _, collection, metrics = metric_fixture(rows)
    result = await metrics.evaluate(fixture["principal"], metric_request())
    assert result.status == "partial"
    assert result.value is None
    assert result.verified_count == 2
    assert result.coverage.unknown_dates == 1
    assert result.period.start_utc == PERIOD_START
    assert result.period.end_utc == PERIOD_END
    assert not collection.write_calls


@pytest.mark.parametrize(
    "mutation", ["tool", "filter", "number", "coverage", "visual", "failure"]
)
def test_scoring_rejects_wrong_results(mutation):
    from tests.test_responses import complete_response

    case = CASES[0]
    response = complete_response()
    tools, arguments = ("count_leads",), {"site": "site_a"}
    if mutation == "tool":
        tools = ("other",)
    elif mutation == "filter":
        arguments = {"site": "site_b"}
    elif mutation == "number":
        response = complete_response(4)
    elif mutation == "coverage":
        response = response.model_copy(
            update={
                "coverage": response.coverage.model_copy(update={"status": "unknown"})
            }
        )
    elif mutation == "visual":
        response = response.model_copy(update={"visuals": ()})
    else:
        case = replace(case, fact=0)
        response = response.model_copy(update={"status": "unavailable"})
    with pytest.raises(AssertionError):
        score(case, tools=tools, arguments=arguments, response=response)


@pytest.mark.asyncio
async def test_denied_site_is_not_read():
    _, service, repository, responses, _, principal, context, command = _setup()
    runner = ScriptedRunner(
        QuestionPlan(intent="count_leads", site="site_b"),
        tool_arguments={"site": "site_b"},
    )
    _service_with_runner(service, runner)
    with pytest.raises(AccessDenied):
        await service.run_turn(principal, context, command)
    assert responses.metric_calls == []
    assert repository.commit_calls == []
