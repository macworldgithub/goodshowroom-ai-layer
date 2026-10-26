"""TICKET-006 aggregate release checks with synthetic evidence only."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.disclosure import (
    DisclosureResult,
    DisclosureService,
    ReleasedAggregate,
    ReviewedPublication,
)
from goodshowroom_ai.metrics import (
    MetricCoverage,
    MetricRegistry,
    MetricResult,
    MetricRequest,
)
from goodshowroom_ai.source_dates import reporting_period
from goodshowroom_ai.storage.identities import Grant, SourceReference
from goodshowroom_ai.storage.settings import IdentityMapping
from tests.test_metrics import (
    FROZEN,
    PERIOD_START,
    cohort_fixture,
    document,
    metric_request,
    service_for as metric_service_for,
    site_settings,
    synthetic_definition,
)


class MetricsStub:
    """Return only the synthetic metric evidence selected by each test."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    async def evaluate(self, principal, request):
        self.calls.append((principal, request))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class AuthorizationProbe:
    """Record DisclosureService checks while using the real policy service."""

    def __init__(self, service):
        self._service = service
        self.requests = []
        self.rechecks = 0

    async def authorize(self, principal, request):
        self.requests.append(request)
        return await self._service.authorize(principal, request)

    async def recheck(self, decision):
        self.rechecks += 1
        return await self._service.recheck(decision)


def complete_evidence(
    value=3,
    *,
    metric_version=1,
    policy_version=7,
    retrieved_at=FROZEN,
    period=None,
    coverage=None,
):
    period = period or reporting_period(
        settings=site_settings(),
        site="site_a",
        now=FROZEN,
        kind="last_seven_days",
    )
    reliable = max(1, value)
    coverage = coverage or MetricCoverage(
        candidates=reliable,
        eligible=reliable,
        excluded=0,
        unknown_eligibility=0,
        reliable_dates=reliable,
        unknown_dates=0,
    )
    return MetricResult(
        metric_id="total_leads",
        metric_version=metric_version,
        site="site_a",
        status="complete",
        value=value,
        verified_count=value,
        period=period,
        coverage=coverage,
        policy_version=policy_version,
        retrieved_at=retrieved_at,
    )


def partial_evidence():
    period = reporting_period(
        settings=site_settings(),
        site="site_a",
        now=FROZEN,
        kind="last_seven_days",
    )
    return MetricResult(
        metric_id="total_leads",
        metric_version=1,
        site="site_a",
        status="partial",
        value=None,
        verified_count=1,
        period=period,
        coverage=MetricCoverage(
            candidates=4,
            eligible=4,
            excluded=0,
            unknown_eligibility=0,
            reliable_dates=3,
            unknown_dates=1,
        ),
        policy_version=7,
        retrieved_at=FROZEN,
        warnings=("received_date_coverage_incomplete",),
    )


def unavailable_evidence():
    return MetricResult(
        metric_id="total_leads",
        metric_version=1,
        site="site_a",
        status="unavailable",
        policy_version=7,
        retrieved_at=FROZEN,
        warnings=("site_timezone_unavailable",),
    )


def publication(
    evidence=None, *, minimum_group=2, review_reference="synthetic-review-v1"
):
    return ReviewedPublication(
        review_reference=review_reference,
        minimum_group=minimum_group,
        evidence=evidence or complete_evidence(),
    )


def disclosure_for(fixture, config, metrics, *, publications=(), authorization=None):
    return DisclosureService(
        metrics=metrics,
        authorization=authorization or fixture["authorization"],
        config=config,
        publications=publications,
    )


def make_context():
    fixture, _binding, config, collection, _repository = cohort_fixture()
    return fixture, config, collection


def change_scope(fixture, mode):
    snapshot = fixture["state"]["snapshot"]
    reference = SourceReference(
        database="byd-leads-new",
        collection="leads",
        key_field="_id",
        key_type="objectid",
        key_value="a" * 24,
    )

    def update_grant(grant):
        selected_records = (reference,) if mode == "selected" else ()
        return Grant(
            **(
                grant.model_dump()
                | {"record_mode": mode, "selected_records": selected_records}
            )
        )

    updated_grants = tuple(update_grant(grant) for grant in snapshot.role.grants)
    updated_scopes = tuple(update_grant(grant) for grant in snapshot.user.scope_limit)
    mappings = snapshot.mappings
    if mode == "assigned":
        verified_at = FROZEN - timedelta(minutes=1)
        mappings = (
            IdentityMapping(
                id=uuid4(),
                user_id=fixture["principal"].user_id,
                source_ref=SourceReference(
                    database="byd-leads-new",
                    collection="users",
                    key_field="_id",
                    key_type="objectid",
                    key_value="b" * 24,
                ),
                active=True,
                sites=("site_a",),
                verified_by=uuid4(),
                verified_at=verified_at,
                created_at=verified_at - timedelta(seconds=1),
                updated_at=verified_at,
            ),
        )
    fixture["state"]["snapshot"] = snapshot.model_copy(
        update={
            "user": snapshot.user.model_copy(update={"scope_limit": updated_scopes}),
            "role": snapshot.role.model_copy(update={"grants": updated_grants}),
            "mappings": mappings,
        }
    )


def narrow_fields(fixture, excluded_field):
    snapshot = fixture["state"]["snapshot"]

    def update(grant):
        return Grant(
            **(
                grant.model_dump()
                | {
                    "fields": tuple(
                        field for field in grant.fields if field != excluded_field
                    )
                }
            )
        )

    fixture["state"]["snapshot"] = snapshot.model_copy(
        update={
            "user": snapshot.user.model_copy(
                update={
                    "scope_limit": tuple(
                        update(grant) for grant in snapshot.user.scope_limit
                    )
                }
            ),
            "role": snapshot.role.model_copy(
                update={
                    "grants": tuple(update(grant) for grant in snapshot.role.grants)
                }
            ),
        }
    )


@pytest.mark.asyncio
async def test_no_reviewed_publication_withholds_without_running_metric_query():
    fixture, _binding, config, collection, repository = cohort_fixture(
        [document(1, "virtualyard", PERIOD_START)]
    )
    metrics = metric_service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )
    service = disclosure_for(fixture, config, metrics)

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.model_dump() == {"status": "withheld", "aggregate": None}
    assert collection.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["inactive_role", "unapproved_user"])
async def test_inactive_role_or_unapproved_user_is_denied_before_metrics(denial):
    fixture, config, _collection = make_context()
    snapshot = fixture["state"]["snapshot"]
    if denial == "inactive_role":
        updated = snapshot.model_copy(
            update={"role": snapshot.role.model_copy(update={"active": False})}
        )
    else:
        updated = snapshot.model_copy(
            update={
                "user": snapshot.user.model_copy(update={"approval_status": "pending"})
            }
        )
    fixture["state"]["snapshot"] = updated
    metrics = MetricsStub(complete_evidence())
    service = disclosure_for(fixture, config, metrics)

    with pytest.raises(AccessDenied):
        await service.evaluate(fixture["principal"], metric_request())

    assert metrics.calls == []


@pytest.mark.asyncio
async def test_q02_is_withheld_without_a_metric_query():
    fixture, config, _collection = make_context()
    evidence = complete_evidence()
    metrics = MetricsStub(evidence)
    service = disclosure_for(
        fixture, config, metrics, publications=(publication(evidence),)
    )

    result = await service.evaluate(
        fixture["principal"], metric_request(question_id="Q-02")
    )

    assert result.model_dump() == {"status": "withheld", "aggregate": None}
    assert metrics.calls == []


@pytest.mark.asyncio
async def test_fresh_disclosure_service_has_no_publication_or_history_by_default():
    fixture, config, _collection = make_context()
    evidence = complete_evidence()
    metrics = MetricsStub(evidence)
    reviewed_service = disclosure_for(
        fixture, config, metrics, publications=(publication(evidence),)
    )
    fresh_service = disclosure_for(fixture, config, metrics)

    released = await reviewed_service.evaluate(fixture["principal"], metric_request())
    after_restart = await fresh_service.evaluate(fixture["principal"], metric_request())

    assert released.status == "released"
    assert after_restart.model_dump() == {"status": "withheld", "aggregate": None}
    assert len(metrics.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("minimum_group", "allowed"), [(2, True), (3, True), (4, False)]
)
async def test_reviewed_minimum_allows_only_complete_positive_evidence_at_or_above_it(
    minimum_group, allowed
):
    fixture, config, _collection = make_context()
    evidence = complete_evidence(value=3)
    metrics = MetricsStub(evidence)

    if not allowed:
        with pytest.raises(ValidationError):
            publication(evidence, minimum_group=minimum_group)
        return

    service = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(evidence, minimum_group=minimum_group),),
    )
    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "released"
    assert result.aggregate.value == 3
    assert len(metrics.calls) == 1


@pytest.mark.parametrize(
    "evidence_factory", [complete_evidence, partial_evidence, unavailable_evidence]
)
def test_zero_partial_and_unavailable_results_cannot_be_reviewed_for_release(
    evidence_factory,
):
    evidence = (
        evidence_factory(value=0)
        if evidence_factory is complete_evidence
        else evidence_factory()
    )

    with pytest.raises(ValidationError):
        publication(evidence, minimum_group=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence_factory", [complete_evidence, partial_evidence, unavailable_evidence]
)
async def test_zero_partial_and_unavailable_live_results_are_withheld(evidence_factory):
    fixture, config, _collection = make_context()
    current = (
        evidence_factory(value=0)
        if evidence_factory is complete_evidence
        else evidence_factory()
    )
    reviewed = publication(complete_evidence(value=3))
    metrics = MetricsStub(current)
    service = disclosure_for(fixture, config, metrics, publications=(reviewed,))

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.model_dump() == {"status": "withheld", "aggregate": None}
    assert len(metrics.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "value",
        "candidates",
        "excluded",
        "policy_version",
        "metric_version",
        "retrieved_at",
        "period",
    ],
)
async def test_any_change_to_published_evidence_is_withheld(change):
    fixture, config, _collection = make_context()
    reviewed = complete_evidence(value=3)
    changed = reviewed
    if change == "value":
        changed = complete_evidence(value=2)
    elif change in {"candidates", "excluded"}:
        coverage = reviewed.coverage
        changed_coverage = MetricCoverage(
            candidates=coverage.candidates + 1,
            eligible=coverage.eligible + int(change == "candidates"),
            excluded=coverage.excluded + int(change == "excluded"),
            unknown_eligibility=coverage.unknown_eligibility,
            reliable_dates=coverage.reliable_dates + int(change == "candidates"),
            unknown_dates=coverage.unknown_dates,
        )
        changed = MetricResult.model_validate(
            reviewed.model_dump() | {"coverage": changed_coverage}
        )
    elif change == "policy_version":
        changed = MetricResult.model_validate(
            reviewed.model_dump() | {"policy_version": reviewed.policy_version + 1}
        )
    elif change == "metric_version":
        changed = MetricResult.model_validate(
            reviewed.model_dump() | {"metric_version": reviewed.metric_version + 1}
        )
    elif change == "retrieved_at":
        changed = MetricResult.model_validate(
            reviewed.model_dump()
            | {"retrieved_at": reviewed.retrieved_at + timedelta(seconds=1)}
        )
    elif change == "period":
        new_period = reporting_period(
            settings=site_settings(),
            site="site_a",
            now=FROZEN + timedelta(days=1),
            kind="last_seven_days",
        )
        changed = MetricResult.model_validate(
            reviewed.model_dump() | {"period": new_period}
        )

    metrics = MetricsStub(changed)
    service = disclosure_for(
        fixture, config, metrics, publications=(publication(reviewed),)
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.model_dump() == {"status": "withheld", "aggregate": None}


def test_duplicate_publication_cells_reject_overlap_and_complementary_thresholds():
    fixture, config, _collection = make_context()
    evidence = complete_evidence(value=3)
    publications = (
        publication(evidence, minimum_group=2, review_reference="review-a"),
        publication(evidence, minimum_group=3, review_reference="review-b"),
    )

    with pytest.raises(ValueError, match="Only one reviewed cell"):
        disclosure_for(
            fixture, config, MetricsStub(evidence), publications=publications
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["selected", "assigned"])
async def test_selected_and_assigned_record_scopes_cannot_release_full_site_publication(
    mode,
):
    fixture, config, _collection = make_context()
    change_scope(fixture, mode)
    evidence = complete_evidence(value=3)
    metrics = MetricsStub(evidence)
    service = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(evidence),),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.model_dump() == {"status": "withheld", "aggregate": None}
    assert metrics.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["site", "metric", "field"])
async def test_disclosure_requires_current_metric_site_and_all_fields(denial):
    fixture, config, _collection = make_context()
    metrics = MetricsStub(complete_evidence())
    request = metric_request()
    if denial == "site":
        request = metric_request(site="site_b")
    elif denial == "metric":
        request = metric_request(metric_id="converted_leads")
    else:
        narrow_fields(fixture, "stage")
    service = disclosure_for(fixture, config, metrics)

    with pytest.raises(AccessDenied, match="^Access is not allowed\\.$"):
        await service.evaluate(fixture["principal"], request)

    assert metrics.calls == []


@pytest.mark.asyncio
async def test_policy_revocation_during_metric_evaluation_denies_final_release():
    fixture, config, _collection = make_context()
    snapshot = fixture["state"]["snapshot"]

    class RevokingMetrics(MetricsStub):
        async def evaluate(self, principal, request):
            result = await super().evaluate(principal, request)
            current = fixture["state"]["snapshot"]
            fixture["state"]["snapshot"] = current.model_copy(
                update={
                    "user": current.user.model_copy(
                        update={"approval_status": "revoked"}
                    )
                }
            )
            return result

    metrics = RevokingMetrics(complete_evidence())
    service = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(complete_evidence()),),
    )

    with pytest.raises(AccessDenied):
        await service.evaluate(fixture["principal"], metric_request())

    assert fixture["state"]["snapshot"] != snapshot
    assert len(metrics.calls) == 1


@pytest.mark.asyncio
async def test_disclosure_authorizes_exact_source_fields_and_rechecks_before_release():
    fixture, config, _collection = make_context()
    authorization = AuthorizationProbe(fixture["authorization"])
    evidence = complete_evidence(value=3)
    service = disclosure_for(
        fixture,
        config,
        MetricsStub(evidence),
        authorization=authorization,
        publications=(publication(evidence),),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    request = authorization.requests[0]
    access = request.data[0]
    assert request.action == "tool"
    assert access.database == "byd-leads-new"
    assert access.collection == "leads"
    assert access.site == "site_a"
    assert access.metric == "total_leads"
    assert access.tool == "count_leads"
    assert access.fields == config.required_fields
    assert result.status == "released"
    assert authorization.rechecks == 1


@pytest.mark.asyncio
async def test_actual_metric_service_releases_only_the_same_reviewed_evidence_repeatedly():
    rows = [document(number, "virtualyard", PERIOD_START) for number in range(1, 4)]
    fixture, _binding, config, collection, repository = cohort_fixture(rows)
    metrics = metric_service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry(
            (synthetic_definition(eligible={"virtualyard"}, excluded=()),)
        ),
    )
    evidence = await metrics.evaluate(fixture["principal"], metric_request())
    assert evidence.status == "complete"
    assert evidence.value == 3
    service = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(evidence),),
    )

    first = await service.evaluate(fixture["principal"], metric_request())
    second = await service.evaluate(fixture["principal"], metric_request())

    assert first.status == second.status == "released"
    assert first.aggregate == second.aggregate
    assert first.aggregate.value == 3
    assert collection.write_calls == []
    assert len(collection.calls) == 3


@pytest.mark.asyncio
async def test_evidence_changes_do_not_build_a_query_history_or_poison_later_equal_release():
    fixture, config, _collection = make_context()
    reviewed = complete_evidence(value=3)
    changed = complete_evidence(value=2)
    metrics = MetricsStub(reviewed, changed, reviewed)
    service = disclosure_for(
        fixture,
        config,
        metrics,
        publications=(publication(reviewed),),
    )

    first = await service.evaluate(fixture["principal"], metric_request())
    changed_result = await service.evaluate(fixture["principal"], metric_request())
    third = await service.evaluate(fixture["principal"], metric_request())

    assert [first.status, changed_result.status, third.status] == [
        "released",
        "withheld",
        "released",
    ]
    assert changed_result.model_dump() == {"status": "withheld", "aggregate": None}
    assert len(metrics.calls) == 3


def test_metric_requests_and_release_contract_reject_breakdowns_or_filter_inputs():
    with pytest.raises(ValidationError):
        MetricRequest.model_validate(
            {
                "metric_id": "total_leads",
                "question_id": "Q-01",
                "site": "site_a",
                "filters": [{"status": "new"}],
            }
        )
    with pytest.raises(ValidationError):
        MetricResult.model_validate(
            complete_evidence().model_dump() | {"groups": [{"status": "new"}]}
        )
    with pytest.raises(ValidationError):
        ReleasedAggregate.model_validate(
            {
                "metric_id": "total_leads",
                "value": 3,
                "site": "site_a",
                "metric_version": 1,
                "policy_version": 7,
                "period": complete_evidence().period,
                "retrieved_at": FROZEN,
                "breakdown": {"status": "new"},
            }
        )


@pytest.mark.asyncio
async def test_text_and_visual_consumers_receive_one_sanitized_aggregate_contract():
    fixture, config, _collection = make_context()
    evidence = complete_evidence(value=3)
    service = disclosure_for(
        fixture,
        config,
        MetricsStub(evidence),
        publications=(publication(evidence),),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    payload = result.model_dump(mode="json")
    assert set(payload) == {"status", "aggregate"}
    assert set(payload["aggregate"]) == {
        "metric_id",
        "value",
        "site",
        "metric_version",
        "policy_version",
        "period",
        "retrieved_at",
        "source",
    }
    assert not {
        "coverage",
        "verified_count",
        "warnings",
        "groups",
        "filters",
    } & set(payload["aggregate"])
    assert (
        DisclosureResult.model_validate_json(result.model_dump_json()).model_dump(
            mode="json"
        )
        == payload
    )
    with pytest.raises(ValidationError):
        DisclosureResult.model_validate(payload | {"visual_dataset": []})
