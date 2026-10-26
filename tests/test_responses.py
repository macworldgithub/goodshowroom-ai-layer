"""TICKET-007 response contracts with frozen synthetic evidence only."""

from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from goodshowroom_ai import responses as response_module
from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.disclosure import DisclosureResult, ReleasedAggregate
from goodshowroom_ai.metrics import MetricRegistry
from goodshowroom_ai.operational import SourceUnavailable
from goodshowroom_ai.responses import (
    AnswerPeriod,
    AnswerScope,
    BuiltResponse,
    Claim,
    Coverage,
    Dataset,
    Evidence,
    Fact,
    MetricRow,
    ResponseBuilder,
    ResponseContext,
    ResponseInvalid,
    Source,
    StructuredResponse,
    Visual,
)
from goodshowroom_ai.source_dates import reporting_period
from tests.test_disclosure import (
    MetricsStub,
    complete_evidence,
    disclosure_for,
    publication,
)
from tests.test_metrics import (
    FROZEN,
    cohort_fixture,
    document,
    metric_request,
    service_for as metric_service_for,
    site_settings,
    synthetic_definition,
)


class DisclosureStub:
    """Return one selected disclosure result or propagate one selected error."""

    def __init__(self, result=None, *, error=None, after_evaluate=None):
        self.result = result if result is not None else DisclosureResult()
        self.error = error
        self.after_evaluate = after_evaluate
        self.calls = []

    async def evaluate(self, principal, request):
        self.calls.append((principal, request))
        if self.after_evaluate is not None:
            self.after_evaluate()
        if self.error is not None:
            raise self.error
        return self.result


def context_for(principal, *, mode="saved", owner_id=None, chat_login_id=None):
    return ResponseContext(
        request_id=uuid4(),
        session_id=uuid4(),
        owner_id=owner_id or principal.user_id,
        mode=mode,
        chat_login_id=(
            principal.login_id
            if chat_login_id is None and mode == "temporary"
            else chat_login_id
        ),
    )


def builder_for(fixture, config, disclosure):
    return ResponseBuilder(
        disclosure=disclosure,
        authorization=fixture["authorization"],
        config=config,
    )


def complete_response(value=3):
    evidence = complete_evidence(value=value)
    period = evidence.period
    answer = f"Total leads: {value}."
    return StructuredResponse(
        status="complete",
        answer=answer,
        claims=(Claim(text=answer),),
        facts=(Fact(value=value, metric_version=evidence.metric_version),),
        datasets=(Dataset(rows=(MetricRow(value=value),)),),
        visuals=(Visual(kind="card"), Visual(kind="table")),
        sources=(Source(),),
        evidence=(Evidence(metric_version=evidence.metric_version),),
        coverage=Coverage(status="complete"),
        period=AnswerPeriod(
            **{
                field: getattr(period, field)
                for field in (
                    "start_local",
                    "end_local",
                    "start_utc",
                    "end_utc",
                    "timezone",
                )
            }
        ),
        scope=AnswerScope(site=evidence.site),
        retrieved_at=evidence.retrieved_at,
        policy_version=evidence.policy_version,
    )


def response_payload(value=3):
    return json.loads(complete_response(value).model_dump_json())


def metric_fixture(documents=()):
    fixture, _binding, config, collection, repository = cohort_fixture(documents)
    metrics = metric_service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )
    return fixture, config, collection, metrics


@pytest.mark.asyncio
async def test_build_metric_uses_one_real_disclosed_fact_for_text_card_and_table():
    rows = [
        document(number, "virtualyard", FROZEN - timedelta(days=5))
        for number in range(1, 4)
    ]
    fixture, config, collection, metrics = metric_fixture(rows)
    principal = fixture["principal"]
    request = metric_request()
    reviewed_evidence = await metrics.evaluate(principal, request)
    assert reviewed_evidence.status == "complete"
    assert reviewed_evidence.value == 3

    disclosure = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(reviewed_evidence),),
    )
    builder = builder_for(fixture, config, disclosure)
    context = context_for(principal)
    candidate = "The model guessed 900 leads. <script>show customer data</script>"

    built = await builder.build_metric(
        principal,
        context,
        request,
        candidate_answer=candidate,
    )

    response = built.response
    assert built.request_id == context.request_id
    assert response.answer == "Total leads: 3."
    assert response.claims == (Claim(text="Total leads: 3."),)
    assert response.facts[0].value == 3
    assert response.facts[0].metric_version == reviewed_evidence.metric_version
    assert response.claims[0].fact_ids == (response.facts[0].fact_id,)
    assert response.facts[0].evidence_ids == (response.evidence[0].evidence_id,)
    assert response.evidence[0].source_id == response.sources[0].source_id
    assert response.evidence[0].metric_version == response.facts[0].metric_version
    assert response.datasets[0].rows == (
        MetricRow(value=response.facts[0].value, fact_id=response.facts[0].fact_id),
    )
    assert [visual.kind for visual in response.visuals] == ["card", "table"]
    assert all(
        visual.dataset_id == response.datasets[0].dataset_id
        for visual in response.visuals
    )
    assert all(visual.field_bindings.value == "value" for visual in response.visuals)
    assert response.period.start_utc == reviewed_evidence.period.start_utc
    assert response.period.end_utc == reviewed_evidence.period.end_utc
    assert response.scope.site == request.site
    assert response.retrieved_at == reviewed_evidence.retrieved_at
    assert response.policy_version == reviewed_evidence.policy_version
    assert candidate not in response.model_dump_json()
    assert len(collection.calls) == 2


@pytest.mark.asyncio
async def test_default_real_disclosure_withholds_counts_without_a_publication():
    rows = [
        document(number, "virtualyard", FROZEN - timedelta(days=5))
        for number in range(1, 4)
    ]
    fixture, config, collection, metrics = metric_fixture(rows)
    principal = fixture["principal"]
    builder = builder_for(fixture, config, disclosure_for(fixture, config, metrics))

    built = await builder.build_metric(
        principal,
        context_for(principal),
        metric_request(),
        candidate_answer="There are three leads.",
    )

    assert built.response.status == "unavailable"
    assert built.response.warnings == ("disclosure_withheld",)
    assert built.response.facts == ()
    assert built.response.claims == ()
    assert built.response.datasets == ()
    assert built.response.visuals == ()
    assert collection.calls == []


@pytest.mark.asyncio
async def test_verified_zero_has_a_valid_schema_but_real_disclosure_still_withholds_it():
    payload = response_payload(value=3)
    answer = "Total leads: 0."
    payload["answer"] = answer
    payload["claims"][0]["text"] = answer
    payload["facts"][0]["value"] = 0
    payload["datasets"][0]["rows"][0]["value"] = 0
    zero_response = StructuredResponse.model_validate_json(json.dumps(payload))
    assert zero_response.status == "complete"
    assert zero_response.answer == answer
    assert zero_response.facts[0].value == zero_response.datasets[0].rows[0].value == 0

    fixture, config, collection, metrics = metric_fixture()
    principal = fixture["principal"]
    zero_evidence = await metrics.evaluate(principal, metric_request())
    assert zero_evidence.status == "complete"
    assert zero_evidence.value == 0
    builder = builder_for(
        fixture,
        config,
        disclosure_for(
            fixture,
            config,
            metrics,
            publications=(publication(complete_evidence(value=2)),),
        ),
    )

    built = await builder.build_metric(
        principal, context_for(principal), metric_request()
    )

    assert built.response.status == "unavailable"
    assert built.response.warnings == ("disclosure_withheld",)
    assert built.response.facts == ()
    assert built.response.datasets == ()
    assert len(collection.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status", "coverage", "warning"),
    [
        ("partial", "partial", "partial", "incomplete_data"),
        ("unavailable", "unavailable", "unknown", "missing_data"),
        ("unsupported", "unsupported", "unknown", "question_not_supported"),
        ("clarification", "clarification", "unknown", "scope_required"),
        ("withheld", "unavailable", "suppressed", "disclosure_withheld"),
    ],
)
async def test_build_notice_returns_distinct_content_free_statuses(
    kind, status, coverage, warning
):
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    disclosure = DisclosureStub()
    builder = builder_for(fixture, config, disclosure)

    built = await builder.build_notice(principal, context_for(principal), kind)

    response = built.response
    assert response.status == status
    assert response.coverage.status == coverage
    assert response.coverage.counts_disclosed is False
    assert response.warnings == (warning,)
    assert response.facts == response.claims == response.datasets == ()
    assert response.visuals == response.sources == response.evidence == ()
    assert response.period is None
    assert response.scope is None
    assert response.retrieved_at is None
    assert all(character not in response.answer for character in "0123456789")
    assert disclosure.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["owner", "temporary_login"])
async def test_chat_owner_and_temporary_login_are_authorized_before_notice(denial):
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    context = (
        context_for(principal, owner_id=uuid4())
        if denial == "owner"
        else context_for(principal, mode="temporary", chat_login_id=uuid4())
    )
    builder = builder_for(fixture, config, DisclosureStub())

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_notice(principal, context, "unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["site", "metric"])
async def test_metric_site_and_metric_are_authorized_before_disclosure(denial):
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    disclosure = DisclosureStub()
    builder = builder_for(fixture, config, disclosure)
    request = (
        metric_request(site="site_b")
        if denial == "site"
        else metric_request(metric_id="converted_leads")
    )

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_metric(principal, context_for(principal), request)

    assert disclosure.calls == []


@pytest.mark.asyncio
async def test_build_metric_rejects_untyped_request_before_authorization_or_disclosure():
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    disclosure = DisclosureStub()
    builder = builder_for(fixture, config, disclosure)

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_metric(
            principal,
            context_for(principal),
            {"metric_id": "total_leads", "site": "site_a"},
        )

    assert disclosure.calls == []


@pytest.mark.asyncio
async def test_policy_change_during_disclosure_denies_the_response():
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    snapshot = fixture["state"]["snapshot"]

    class RevokingMetrics(MetricsStub):
        async def evaluate(self, caller, request):
            result = await super().evaluate(caller, request)
            current = fixture["state"]["snapshot"]
            fixture["state"]["snapshot"] = current.model_copy(
                update={
                    "user": current.user.model_copy(
                        update={"approval_status": "revoked"}
                    )
                }
            )
            return result

    evidence = complete_evidence(value=3)
    disclosure = disclosure_for(
        fixture,
        config,
        RevokingMetrics(evidence),
        publications=(publication(evidence),),
    )
    builder = builder_for(fixture, config, disclosure)

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_metric(
            principal,
            context_for(principal),
            metric_request(),
            candidate_answer="PRIVATE CUSTOMER SENTINEL",
        )

    assert fixture["state"]["snapshot"] != snapshot


@pytest.mark.asyncio
async def test_final_policy_recheck_denies_a_change_after_disclosure_returns():
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]

    def change_policy():
        snapshot = fixture["state"]["snapshot"]
        fixture["state"]["snapshot"] = snapshot.model_copy(
            update={"policy_version": snapshot.policy_version + 1}
        )

    disclosure = DisclosureStub(after_evaluate=change_policy)
    builder = builder_for(fixture, config, disclosure)

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_metric(principal, context_for(principal), metric_request())

    assert len(disclosure.calls) == 1


@pytest.mark.asyncio
async def test_disclosure_access_denial_propagates_as_an_error():
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    disclosure = DisclosureStub(error=AccessDenied("Access is not allowed."))
    builder = builder_for(fixture, config, disclosure)

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_metric(
            principal,
            context_for(principal),
            metric_request(),
            candidate_answer="PRIVATE MODEL SENTINEL",
        )


@pytest.mark.asyncio
async def test_source_failure_propagates_without_becoming_a_success_notice_or_leaking_details():
    rows = [
        document(number, "virtualyard", FROZEN - timedelta(days=5))
        for number in range(1, 4)
    ]
    fixture, config, collection, metrics = metric_fixture(rows)
    collection.aggregate_error = RuntimeError(
        "mongodb://secret.invalid/customer-sentinel"
    )
    disclosure = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(complete_evidence(value=3)),),
    )
    builder = builder_for(fixture, config, disclosure)

    with pytest.raises(SourceUnavailable, match=r"^Metric is unavailable\.$") as error:
        await builder.build_metric(
            fixture["principal"],
            context_for(fixture["principal"]),
            metric_request(),
            candidate_answer="PRIVATE MODEL SENTINEL",
        )

    assert "secret.invalid" not in str(error.value)
    assert "customer-sentinel" not in str(error.value)
    assert len(collection.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["site", "policy_version"])
async def test_released_evidence_with_site_or_policy_mismatch_is_denied(mismatch):
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    site = "site_b" if mismatch == "site" else "site_a"
    period = reporting_period(
        settings=site_settings(),
        site=site,
        now=FROZEN,
        kind="last_seven_days",
    )
    result = DisclosureResult(
        status="released",
        aggregate=ReleasedAggregate(
            value=3,
            site=site,
            metric_version=1,
            policy_version=8 if mismatch == "policy_version" else 7,
            period=period,
            retrieved_at=FROZEN,
        ),
    )
    builder = builder_for(fixture, config, DisclosureStub(result))

    with pytest.raises(AccessDenied, match=r"^Access is not allowed\.$"):
        await builder.build_metric(principal, context_for(principal), metric_request())


@pytest.mark.asyncio
async def test_malformed_disclosure_is_rejected_with_a_safe_response_error():
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    period = complete_evidence().period
    invalid_aggregate = ReleasedAggregate(
        value=3,
        site="site_a",
        metric_version=1,
        policy_version=7,
        period=period,
        retrieved_at=FROZEN,
    ).model_copy(update={"source": "RAW_SENTINEL_SOURCE"})
    malformed = DisclosureResult(
        status="released",
        aggregate=invalid_aggregate,
    )
    builder = builder_for(fixture, config, DisclosureStub(malformed))

    with pytest.raises(ResponseInvalid, match=r"^Response is invalid\.$") as error:
        await builder.build_metric(
            principal,
            context_for(principal),
            metric_request(),
            candidate_answer="PRIVATE MODEL SENTINEL",
        )

    assert "RAW_SENTINEL_SOURCE" not in str(error.value)
    assert "PRIVATE MODEL SENTINEL" not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "unsupported_answer_number",
        "number_word",
        "html_answer",
        "claim_text",
        "claim_reference",
        "fact_reference",
        "fact_evidence_reference",
        "fact_value",
        "dataset_fact_reference",
        "dataset_value",
        "visual_binding",
        "evidence_version",
        "evidence_source_reference",
        "source_reference",
        "coverage_status",
        "retrieved_before_period_end",
    ],
)
def test_json_roundtrip_rejects_unverified_numbers_and_broken_response_graph(mutation):
    payload = response_payload()
    if mutation == "unsupported_answer_number":
        payload["answer"] = "Total leads: 4."
    elif mutation == "number_word":
        payload["answer"] = "Total leads: three."
    elif mutation == "html_answer":
        payload["answer"] = "<script>3</script>"
    elif mutation == "claim_text":
        payload["claims"][0]["text"] = "Total leads: 4."
    elif mutation == "claim_reference":
        payload["claims"][0]["fact_ids"] = ["fact_unverified"]
    elif mutation == "fact_reference":
        payload["facts"][0]["fact_id"] = "fact_unverified"
    elif mutation == "fact_evidence_reference":
        payload["facts"][0]["evidence_ids"] = ["evidence_unverified"]
    elif mutation == "fact_value":
        payload["facts"][0]["value"] = 4
    elif mutation == "dataset_fact_reference":
        payload["datasets"][0]["rows"][0]["fact_id"] = "fact_unverified"
    elif mutation == "dataset_value":
        payload["datasets"][0]["rows"][0]["value"] = 4
    elif mutation == "visual_binding":
        payload["visuals"][0]["field_bindings"]["value"] = "untrusted_value"
    elif mutation == "evidence_version":
        payload["evidence"][0]["metric_version"] += 1
    elif mutation == "evidence_source_reference":
        payload["evidence"][0]["source_id"] = "source_unverified"
    elif mutation == "source_reference":
        payload["sources"][0]["source_id"] = "source_unverified"
    elif mutation == "coverage_status":
        payload["coverage"]["status"] = "partial"
    else:
        payload["retrieved_at"] = (
            AnswerPeriod.model_validate_json(json.dumps(payload["period"])).end_utc
            - timedelta(seconds=1)
        ).isoformat()

    with pytest.raises(ValidationError):
        StructuredResponse.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "invalid_value",
    [3.0, True, -1, float("nan")],
    ids=["float", "boolean", "negative", "nan"],
)
def test_json_roundtrip_rejects_noninteger_or_nonfinite_metric_values(invalid_value):
    payload = response_payload()
    payload["facts"][0]["value"] = invalid_value
    payload["datasets"][0]["rows"][0]["value"] = invalid_value

    with pytest.raises(ValidationError):
        StructuredResponse.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "too_many_claims",
        "too_many_visuals",
        "long_answer",
        "boolean_policy",
    ],
)
def test_json_roundtrip_rejects_unknown_fields_and_response_bounds(mutation):
    payload = response_payload()
    if mutation == "unknown_field":
        payload["model_configuration"] = {"temperature": 0}
    elif mutation == "too_many_claims":
        payload["claims"].append(deepcopy(payload["claims"][0]))
    elif mutation == "too_many_visuals":
        payload["visuals"].append(deepcopy(payload["visuals"][0]))
    elif mutation == "long_answer":
        payload["answer"] = "x" * 1001
    else:
        payload["policy_version"] = True

    with pytest.raises(ValidationError):
        StructuredResponse.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "invalid_period",
    ["timezone", "calendar_span", "nonpositive_utc", "unknown_zone", "not_midnight"],
)
def test_answer_period_rejects_timezone_or_boundary_mismatches(invalid_period):
    payload = json.loads(complete_response().period.model_dump_json())
    if invalid_period == "timezone":
        payload["timezone"] = "UTC"
    elif invalid_period == "calendar_span":
        valid_period = AnswerPeriod.model_validate_json(json.dumps(payload))
        end_local = valid_period.end_local + timedelta(days=1)
        end_utc = valid_period.end_utc + timedelta(days=1)
        payload["end_local"] = end_local.isoformat()
        payload["end_utc"] = end_utc.isoformat()
    elif invalid_period == "nonpositive_utc":
        start_local = payload["start_local"]
        payload["end_local"] = start_local
        payload["end_utc"] = payload["start_utc"]
    elif invalid_period == "not_midnight":
        valid_period = AnswerPeriod.model_validate_json(json.dumps(payload))
        payload["start_local"] = (
            valid_period.start_local + timedelta(seconds=1)
        ).isoformat()
        payload["start_utc"] = (
            valid_period.start_utc + timedelta(seconds=1)
        ).isoformat()
    else:
        payload["timezone"] = "Not/A_Real_Zone"

    with pytest.raises(ValueError):
        AnswerPeriod.model_validate_json(json.dumps(payload))


def test_answer_period_accepts_matching_iana_timezone_and_seven_local_midnights():
    valid = complete_response().period
    assert valid.timezone == "Australia/Melbourne"
    assert valid.end_local.date() - valid.start_local.date() == timedelta(days=7)
    assert (valid.start_local.hour, valid.start_local.minute) == (0, 0)
    assert (valid.end_local.hour, valid.end_local.minute) == (0, 0)
    assert (valid.start_local.second, valid.start_local.microsecond) == (0, 0)
    assert (valid.end_local.second, valid.end_local.microsecond) == (0, 0)
    assert valid.end_utc > valid.start_utc


@pytest.mark.asyncio
async def test_builder_enforces_encoded_response_byte_cap(monkeypatch):
    fixture, _binding, config, _collection, _repository = cohort_fixture()
    principal = fixture["principal"]
    builder = builder_for(fixture, config, DisclosureStub())
    monkeypatch.setattr(response_module, "MAX_RESPONSE_BYTES", 8)

    with pytest.raises(ResponseInvalid, match=r"^Response is invalid\.$"):
        await builder.build_notice(principal, context_for(principal), "unavailable")


def test_built_response_keeps_request_id_and_closed_response_contract():
    context_id = uuid4()
    built = BuiltResponse(
        request_id=context_id,
        response=complete_response(),
    )
    roundtrip = BuiltResponse.model_validate_json(built.model_dump_json())
    assert roundtrip.request_id == context_id
    assert roundtrip.response == built.response
