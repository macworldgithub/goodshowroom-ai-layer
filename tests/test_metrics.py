"""Synthetic TICKET-005 count fixtures with an independently evaluated pipeline."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from bson import Int64, ObjectId
from pydantic import ValidationError

from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.metrics import (
    MetricDefinition,
    MetricRegistry,
    MetricRequest,
    MetricService,
)
from goodshowroom_ai.normalization import PlatformDateRule, build_lead_adapter
from goodshowroom_ai.operational import (
    OperationalReadRepository,
    SourceUnavailable,
    _validate_pipeline,
)
from goodshowroom_ai.operational_models import (
    DateCohortCounts,
    DateCohortPlan,
    DateFilter,
    EqualFilter,
    ReadRequest,
)
from goodshowroom_ai.source_dates import DateRule
from goodshowroom_ai.storage.settings import AppSetting, SiteConfiguration
from tests.test_operational_reads import (
    FakeCollection,
    FakeCursor,
    FakeDatabase,
    policy_fixture,
)

FROZEN = datetime(2026, 10, 22, 9, tzinfo=UTC)
PERIOD_START = datetime(2026, 10, 14, 13, tzinfo=UTC)
PERIOD_END = datetime(2026, 10, 21, 13, tzinfo=UTC)
SITE_A_VALUE = "dealer-code-a"
SITE_B_VALUE = "dealer-code-b"
_MISSING = object()


def site_settings(*, timezone="Australia/Melbourne"):
    return AppSetting(
        id="sites",
        sites=(
            SiteConfiguration(
                site_id="site_a", aliases=(SITE_A_VALUE,), timezone=timezone
            ),
            SiteConfiguration(
                site_id="site_b", aliases=(SITE_B_VALUE,), timezone="UTC"
            ),
        ),
        created_at=FROZEN,
        updated_at=FROZEN,
    )


def synthetic_definition(
    *,
    eligible=None,
    excluded=("manual",),
    identity_review="synthetic-id-review-v1",
    exclusions_review="synthetic-exclusion-review-v1",
):
    eligible = eligible or {"virtualyard", "dealer_studio", "autogate", "sms"}
    date_rules = tuple(
        rule
        for rule in (
            PlatformDateRule(
                platform="virtualyard",
                rule=DateRule(
                    provenance="original", review_reference="synthetic-vy-date-v1"
                ),
            ),
            PlatformDateRule(
                platform="dealer_studio",
                rule=DateRule(
                    provenance="native", review_reference="synthetic-ds-date-v1"
                ),
            ),
            PlatformDateRule(
                platform="autogate",
                rule=DateRule(
                    provenance="imported", review_reference="synthetic-ag-date-v1"
                ),
            ),
            PlatformDateRule(
                platform="sms",
                rule=DateRule(
                    provenance="test_drive", review_reference="synthetic-sms-date-v1"
                ),
            ),
        )
        if rule.platform in eligible
    )
    return MetricDefinition(
        metric_id="total_leads",
        enabled=True,
        meaning="Synthetic reviewed lead count fixture.",
        identity="distinct_source_records",
        identity_review=identity_review,
        exclusions_review=exclusions_review,
        eligible_platforms=frozenset(eligible),
        excluded_platforms=frozenset(excluded),
        date_rules=date_rules,
    )


def document(number, platform, created, *, site=SITE_A_VALUE):
    return {
        "_id": ObjectId(f"{number:024x}"),
        "dealer": site,
        "platform": platform,
        "leadCreatedDate": created,
        "status": "new",
        "stage": "NEW ENQUIRIES",
    }


def cohort_fixture(documents=(), *, timezone="Australia/Melbourne", loader=None):
    settings = site_settings(timezone=timezone)
    binding, config = build_lead_adapter(settings)
    fixture = policy_fixture(
        bindings=(binding,),
        allowed_tools={binding.name: ("count_leads",)},
        allowed_metrics=("total_leads",),
        grant_sites=("site_a",),
    )
    if loader is not None:
        fixture = policy_fixture(
            bindings=(binding,),
            allowed_tools={binding.name: ("count_leads",)},
            allowed_metrics=("total_leads",),
            grant_sites=("site_a",),
            loader=loader,
        )
    snapshot = fixture["state"]["snapshot"].model_copy(update={"sites": settings})
    fixture["state"]["snapshot"] = snapshot
    fixture["snapshot"] = snapshot
    database = FakeDatabase("byd-leads-new")
    collection = EvaluatingCollection(documents)
    database.collections["leads"] = collection
    repository = OperationalReadRepository(
        databases={"byd-leads-new": database},
        authorization=fixture["authorization"],
        bindings=(binding,),
    )
    return fixture, binding, config, collection, repository


class EvaluatingCollection(FakeCollection):
    """Run only the generated match/facet/count subset over fixed Python records."""

    def __init__(self, documents=()):
        super().__init__()
        self.documents = list(documents)

    async def aggregate(self, pipeline, **kwargs):
        self.calls.append((deepcopy(pipeline), kwargs))
        if self.on_aggregate is not None:
            self.on_aggregate()
        if self.aggregate_error is not None:
            raise self.aggregate_error
        response = (
            self.responses.pop(0)
            if self.responses
            else evaluate_cohort(self.documents, pipeline)
        )
        cursor = response if isinstance(response, FakeCursor) else FakeCursor(response)
        self.cursors.append(cursor)
        return cursor


def _path(document_value, path):
    value = document_value
    for part in path[1:].split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _bson_type(value):
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if type(value) is str:
        return "string"
    if type(value) is datetime:
        return "date"
    if isinstance(value, ObjectId):
        return "objectId"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if isinstance(value, Int64):
        return "long"
    if type(value) is float:
        return "double"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise AssertionError("Unsupported value in synthetic BSON fixture")


def _operand(expression, row):
    if type(expression) is str and expression.startswith("$"):
        return _path(row, expression)
    if isinstance(expression, list):
        return [_operand(item, row) for item in expression]
    if isinstance(expression, dict) and set(expression) == {"$type"}:
        return _bson_type(_operand(expression["$type"], row))
    return expression


def _expression(expression, row):
    assert isinstance(expression, dict) and len(expression) == 1
    operator, operand = next(iter(expression.items()))
    if operator == "$and":
        return all(_expression(item, row) for item in operand)
    if operator == "$eq":
        left, right = (_operand(item, row) for item in operand)
        return left is not _MISSING and right is not _MISSING and left == right
    if operator == "$in":
        value, options = (_operand(item, row) for item in operand)
        return value is not _MISSING and value in options
    if operator == "$gte":
        left, right = (_operand(item, row) for item in operand)
        return left is not _MISSING and right is not _MISSING and left >= right
    if operator == "$lt":
        left, right = (_operand(item, row) for item in operand)
        return left is not _MISSING and right is not _MISSING and left < right
    raise AssertionError(f"The fake database does not support {operator}")


def _matches(query, row):
    for key, condition in query.items():
        if key == "$and":
            if not all(_matches(item, row) for item in condition):
                return False
        elif key == "$or":
            if not any(_matches(item, row) for item in condition):
                return False
        elif key == "$expr":
            if not _expression(condition, row):
                return False
        else:
            value = row.get(key, _MISSING)
            if isinstance(condition, dict):
                assert set(condition) == {"$in"}
                if value is _MISSING or value not in condition["$in"]:
                    return False
            elif value is _MISSING or value != condition:
                return False
    return True


def evaluate_cohort(documents, pipeline):
    """Evaluate scope before facets; return the same bounded shape as Mongo."""
    assert len(pipeline) == 3
    assert set(pipeline[0]) == {"$match"}
    assert set(pipeline[1]) == {"$project"}
    assert pipeline[1]["$project"] == {"_id": 1, "platform": 1, "leadCreatedDate": 1}
    assert set(pipeline[2]) == {"$facet"}
    scoped = [row for row in documents if _matches(pipeline[0]["$match"], row)]
    projected = [
        {
            key: row[key]
            for key, include in pipeline[1]["$project"].items()
            if include and key in row
        }
        for row in scoped
    ]
    facets = pipeline[2]["$facet"]
    assert set(facets) == {
        "candidates",
        "eligible",
        "excluded",
        "reliable",
        "in_period",
    }
    output = {}
    for name, stages in facets.items():
        assert stages[-1] == {"$count": "count"}
        matched = projected
        for stage in stages[:-1]:
            assert set(stage) == {"$match"}
            matched = [row for row in matched if _matches(stage["$match"], row)]
        output[name] = [{"count": len(matched)}] if matched else []
    return [output]


def service_for(fixture, repository, config, *, registry=None, now=FROZEN):
    return MetricService(
        repository=repository,
        authorization=fixture["authorization"],
        config=config,
        clock=lambda: now,
        registry=registry,
    )


def metric_request(*, metric_id="total_leads", question_id="Q-01", site="site_a"):
    return MetricRequest(metric_id=metric_id, question_id=question_id, site=site)


def test_default_registry_is_unsupported_and_does_not_query_unreviewed_sources():
    fixture, _binding, config, collection, repository = cohort_fixture(
        [document(1, "virtualyard", PERIOD_START)]
    )
    service = service_for(fixture, repository, config)

    result = run(service.evaluate(fixture["principal"], metric_request()))

    assert result.status == "unsupported"
    assert result.value is None
    assert result.coverage is None
    assert result.warnings == ("identity_or_exclusions_unreviewed",)
    assert collection.calls == []


@pytest.mark.parametrize("question_id", ["Q-02", "Q-03", "Q-04", "Q-05"])
@pytest.mark.asyncio
async def test_later_question_families_remain_disabled(question_id):
    fixture, _binding, config, collection, repository = cohort_fixture()
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )

    result = await service.evaluate(
        fixture["principal"], metric_request(question_id=question_id)
    )

    assert result.status == "unsupported"
    assert result.warnings == ("question_family_not_enabled",)
    assert collection.calls == []


def test_converted_metric_is_explicitly_disabled_and_cannot_be_enabled():
    default = MetricRegistry().get("converted_leads")
    assert default is not None
    assert default.enabled is False
    assert default.question_id == "deferred"

    with pytest.raises(ValidationError):
        MetricDefinition(
            metric_id="converted_leads",
            enabled=True,
            meaning="Synthetic attempt to enable undefined conversion meaning.",
            identity="distinct_source_records",
            identity_review="synthetic-id-review-v1",
            exclusions_review="synthetic-exclusion-review-v1",
        )


@pytest.mark.parametrize(
    "metric_id,identity_review,exclusions_review",
    [
        ("unknown_metric", "synthetic-id-review-v1", "synthetic-exclusion-review-v1"),
        ("total_leads", None, "synthetic-exclusion-review-v1"),
        ("total_leads", "synthetic-id-review-v1", None),
    ],
)
@pytest.mark.asyncio
async def test_unknown_or_unreviewed_metric_has_no_source_read(
    metric_id, identity_review, exclusions_review
):
    fixture, _binding, config, collection, repository = cohort_fixture()
    definition = synthetic_definition(
        identity_review=identity_review, exclusions_review=exclusions_review
    )
    service = service_for(
        fixture, repository, config, registry=MetricRegistry((definition,))
    )

    result = await service.evaluate(
        fixture["principal"], metric_request(metric_id=metric_id)
    )

    assert result.status == "unsupported"
    assert result.value is None
    assert collection.calls == []


@pytest.mark.asyncio
async def test_q01_counts_only_verified_original_native_dates_and_keeps_unknowns_visible():
    documents = [
        document(1, "virtualyard", PERIOD_START),
        document(2, "dealer_studio", PERIOD_END - timedelta(microseconds=1)),
        document(3, "autogate", PERIOD_END - timedelta(hours=1)),
        document(4, "virtualyard", None),
        document(5, "manual", PERIOD_START),
        document(6, "mystery_importer", PERIOD_START),
        document(7, "sms", PERIOD_START),
        document(8, "dealer_studio", "2026-10-18T10:00:00Z"),
        document(9, "virtualyard", PERIOD_START, site=SITE_B_VALUE),
    ]
    fixture, _binding, config, collection, repository = cohort_fixture(documents)
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "partial"
    assert result.value is None
    assert result.verified_count == 2
    assert result.period is not None
    assert result.period.timezone == "Australia/Melbourne"
    assert result.period.start_local == datetime(
        2026, 10, 15, 0, tzinfo=ZoneInfo("Australia/Melbourne")
    )
    assert result.period.end_local == datetime(
        2026, 10, 22, 0, tzinfo=ZoneInfo("Australia/Melbourne")
    )
    assert (result.period.start_utc, result.period.end_utc) == (
        PERIOD_START,
        PERIOD_END,
    )
    assert result.coverage.model_dump() == {
        "scope": "full_authorized_query",
        "candidates": 8,
        "eligible": 6,
        "excluded": 1,
        "unknown_eligibility": 1,
        "reliable_dates": 2,
        "unknown_dates": 4,
    }
    assert result.warnings == (
        "unknown_source_eligibility",
        "received_date_coverage_incomplete",
    )
    assert len(collection.calls) == 1
    pipeline = collection.calls[0][0]
    assert pipeline[0]["$match"]["$and"][0]["$and"][2] == {
        "dealer": {"$in": ["site_a", SITE_A_VALUE]}
    }
    assert list(pipeline[2]["$facet"]) == [
        "candidates",
        "eligible",
        "excluded",
        "reliable",
        "in_period",
    ]
    reliable_expression = pipeline[2]["$facet"]["reliable"][0]["$match"]["$expr"]
    assert "virtualyard" in repr(reliable_expression)
    assert "dealer_studio" in repr(reliable_expression)
    assert "autogate" not in repr(reliable_expression)
    assert "sms" not in repr(reliable_expression)
    assert collection.write_calls == []


@pytest.mark.asyncio
async def test_verified_zero_is_an_exact_complete_result():
    documents = [document(1, "virtualyard", PERIOD_END)]
    fixture, _binding, config, collection, repository = cohort_fixture(documents)
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry(
            (synthetic_definition(eligible={"virtualyard"}, excluded=()),)
        ),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "complete"
    assert result.value == 0
    assert result.verified_count == 0
    assert result.coverage.candidates == result.coverage.eligible == 1
    assert result.coverage.reliable_dates == 1
    assert result.coverage.unknown_dates == result.coverage.unknown_eligibility == 0
    assert collection.calls[0][0][0]["$match"]


@pytest.mark.asyncio
async def test_all_unknown_dates_are_unavailable_not_zero():
    documents = [
        document(1, "virtualyard", None),
        document(2, "virtualyard", "2026-10-18T10:00:00Z"),
    ]
    fixture, _binding, config, _collection, repository = cohort_fixture(documents)
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry(
            (synthetic_definition(eligible={"virtualyard"}, excluded=()),)
        ),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "unavailable"
    assert result.value is None
    assert result.verified_count is None
    assert result.coverage.candidates == 2
    assert result.coverage.eligible == 2
    assert result.coverage.reliable_dates == 0
    assert result.coverage.unknown_dates == 2


@pytest.mark.asyncio
async def test_full_count_above_page_size_uses_one_fixed_five_facet_query():
    documents = [document(index, "virtualyard", PERIOD_START) for index in range(1, 36)]
    fixture, _binding, config, collection, repository = cohort_fixture(documents)
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry(
            (synthetic_definition(eligible={"virtualyard"}, excluded=()),)
        ),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "complete"
    assert result.value == 35
    assert result.coverage.candidates == 35
    assert len(collection.calls) == 1
    pipeline, options = collection.calls[0]
    assert len(pipeline) == 3
    assert pipeline[1] == {"$project": {"_id": 1, "platform": 1, "leadCreatedDate": 1}}
    assert set(pipeline[2]["$facet"]) == {
        "candidates",
        "eligible",
        "excluded",
        "reliable",
        "in_period",
    }
    assert not any(
        isinstance(stage, dict)
        and any(operator in stage for operator in ("$limit", "$sum", "$lookup"))
        for stage in walk(pipeline)
    )
    assert options["allowDiskUse"] is False


@pytest.mark.asyncio
async def test_dst_calendar_boundaries_use_seven_local_days_and_exact_half_open_count():
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    dst_start = datetime(2026, 9, 29, 14, tzinfo=UTC)
    dst_end = datetime(2026, 10, 6, 13, tzinfo=UTC)
    documents = [
        document(1, "virtualyard", dst_start),
        document(2, "virtualyard", dst_end - datetime.resolution),
        document(3, "virtualyard", dst_end),
    ]
    fixture, _binding, config, _collection, repository = cohort_fixture(documents)
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry(
            (synthetic_definition(eligible={"virtualyard"}, excluded=()),)
        ),
        now=now,
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "complete"
    assert result.value == 2
    assert result.period.start_local == datetime(
        2026, 9, 30, 0, tzinfo=ZoneInfo("Australia/Melbourne")
    )
    assert result.period.end_local == datetime(
        2026, 10, 7, 0, tzinfo=ZoneInfo("Australia/Melbourne")
    )
    assert (result.period.start_utc, result.period.end_utc) == (dst_start, dst_end)
    assert result.period.end_utc - result.period.start_utc == timedelta(hours=167)


@pytest.mark.asyncio
async def test_missing_site_timezone_is_unavailable_without_a_source_query():
    fixture, _binding, config, collection, repository = cohort_fixture(
        [document(1, "virtualyard", PERIOD_START)], timezone=None
    )
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )

    result = await service.evaluate(fixture["principal"], metric_request())

    assert result.status == "unavailable"
    assert result.warnings == ("site_timezone_unavailable",)
    assert result.coverage is None
    assert collection.calls == []


@pytest.mark.asyncio
async def test_authorization_rejects_another_site_before_query():
    fixture, _binding, config, collection, repository = cohort_fixture(
        [document(1, "virtualyard", PERIOD_START, site=SITE_B_VALUE)]
    )
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )

    with pytest.raises(AccessDenied):
        await service.evaluate(fixture["principal"], metric_request(site="site_b"))

    assert collection.calls == []


def test_date_cohort_plan_rejects_operator_strings_and_source_partition_overlap():
    with pytest.raises(ValidationError):
        DateCohortPlan(
            platform_field="platform",
            date_field="leadCreatedDate",
            eligible_platforms=frozenset({"$where"}),
            period=DateFilter(
                field="leadCreatedDate", start=PERIOD_START, end=PERIOD_END
            ),
        )

    with pytest.raises(ValidationError):
        DateCohortPlan(
            platform_field="platform",
            date_field="leadCreatedDate",
            eligible_platforms=frozenset({"virtualyard"}),
            excluded_platforms=frozenset({"virtualyard"}),
            period=DateFilter(
                field="leadCreatedDate", start=PERIOD_START, end=PERIOD_END
            ),
        )


@pytest.mark.parametrize("field_change", ["namespace", "platform_path", "date_path"])
@pytest.mark.asyncio
async def test_cohort_reader_rejects_wrong_namespace_or_source_path_before_aggregate(
    field_change,
):
    fixture, binding, config, collection, _repository = cohort_fixture()
    if field_change == "namespace":
        binding = binding.model_copy(update={"database": "byd-panel"})
        database = FakeDatabase("byd-panel")
    else:
        changed = tuple(
            field.model_copy(
                update={
                    "path": "raw_platform"
                    if field.name == "platform" and field_change == "platform_path"
                    else "created"
                }
            )
            if field.name
            == ("platform" if field_change == "platform_path" else "leadCreatedDate")
            else field
            for field in binding.fields
        )
        binding = binding.model_copy(update={"fields": changed})
        database = FakeDatabase("byd-leads-new")
    database.collections["leads"] = collection
    repository = OperationalReadRepository(
        databases={database.name: database},
        authorization=fixture["authorization"],
        bindings=(binding,),
    )
    plan = DateCohortPlan(
        platform_field=config.platform_column,
        date_field=config.date_column,
        eligible_platforms=frozenset({"virtualyard"}),
        reliable_platforms=frozenset({"virtualyard"}),
        period=DateFilter(field=config.date_column, start=PERIOD_START, end=PERIOD_END),
    )
    read = ReadRequest(
        binding=config.binding,
        site="site_a",
        tool="count_leads",
        fields=config.required_fields,
        metric="total_leads",
    )

    with pytest.raises(SourceUnavailable, match="^Source is unavailable\\.$"):
        await repository.count_date_cohort(fixture["principal"], read, plan=plan)

    assert collection.calls == []


@pytest.mark.parametrize("request_change", ["filter", "date_filter"])
@pytest.mark.asyncio
async def test_cohort_reader_rejects_non_site_filters(request_change):
    fixture, _binding, config, collection, repository = cohort_fixture()
    plan = DateCohortPlan(
        platform_field=config.platform_column,
        date_field=config.date_column,
        eligible_platforms=frozenset({"virtualyard"}),
        reliable_platforms=frozenset({"virtualyard"}),
        period=DateFilter(field=config.date_column, start=PERIOD_START, end=PERIOD_END),
    )
    kwargs = (
        {"filters": (EqualFilter(field="status", value="new"),)}
        if request_change == "filter"
        else {
            "dates": DateFilter(
                field=config.date_column, start=PERIOD_START, end=PERIOD_END
            )
        }
    )
    read_values = {
        "binding": config.binding,
        "site": "site_a",
        "tool": "count_leads",
        "fields": config.required_fields,
        "metric": "total_leads",
    }
    read = ReadRequest(**(read_values | kwargs))

    with pytest.raises(SourceUnavailable):
        await repository.count_date_cohort(fixture["principal"], read, plan=plan)

    assert collection.calls == []


@pytest.mark.parametrize("request_change", ["tool", "metric"])
@pytest.mark.asyncio
async def test_cohort_reader_requires_the_named_q01_tool_and_metric(request_change):
    fixture, _binding, config, collection, repository = cohort_fixture()
    plan = DateCohortPlan(
        platform_field=config.platform_column,
        date_field=config.date_column,
        eligible_platforms=frozenset({"virtualyard"}),
        reliable_platforms=frozenset({"virtualyard"}),
        period=DateFilter(field=config.date_column, start=PERIOD_START, end=PERIOD_END),
    )
    kwargs = (
        {"tool": "lead_list"}
        if request_change == "tool"
        else {"metric": "converted_leads"}
    )
    read_values = {
        "binding": config.binding,
        "site": "site_a",
        "tool": "count_leads",
        "fields": config.required_fields,
        "metric": "total_leads",
    }
    read = ReadRequest(**(read_values | kwargs))

    with pytest.raises(SourceUnavailable):
        await repository.count_date_cohort(fixture["principal"], read, plan=plan)

    assert collection.calls == []


def _count_document(**counts):
    return {
        key: ([{"count": value}] if value is not None else [])
        for key, value in (
            {
                "candidates": 3,
                "eligible": 2,
                "excluded": 1,
                "reliable": 2,
                "in_period": 1,
            }
            | counts
        ).items()
    }


async def _direct_count_fixture(response):
    fixture, binding, config, collection, repository = cohort_fixture()
    collection.responses = [[response]]
    plan = DateCohortPlan(
        platform_field=config.platform_column,
        date_field=config.date_column,
        eligible_platforms=frozenset({"virtualyard"}),
        reliable_platforms=frozenset({"virtualyard"}),
        period=DateFilter(field=config.date_column, start=PERIOD_START, end=PERIOD_END),
    )
    read = ReadRequest(
        binding=config.binding,
        site="site_a",
        tool="count_leads",
        fields=config.required_fields,
        metric="total_leads",
    )
    return fixture, collection, repository, read, plan


@pytest.mark.parametrize(
    "key,value",
    [
        ("candidates", True),
        ("eligible", 2.5),
        ("excluded", -1),
        ("reliable", None),
    ],
)
@pytest.mark.asyncio
async def test_malformed_facet_counts_fail_closed(key, value):
    response = _count_document(**{key: value})
    fixture, collection, repository, read, plan = await _direct_count_fixture(response)

    with pytest.raises(SourceUnavailable, match="^Source is unavailable\\.$"):
        await repository.count_date_cohort(fixture["principal"], read, plan=plan)

    assert len(collection.calls) == 1


@pytest.mark.parametrize(
    "counts",
    [
        {"eligible": 3},
        {"reliable": 3},
        {"in_period": 3},
    ],
)
@pytest.mark.asyncio
async def test_inconsistent_facet_partitions_fail_closed(counts):
    fixture, _collection, repository, read, plan = await _direct_count_fixture(
        _count_document(**counts)
    )

    with pytest.raises(SourceUnavailable):
        await repository.count_date_cohort(fixture["principal"], read, plan=plan)


@pytest.mark.asyncio
async def test_bson_int64_facet_counts_are_accepted_as_python_integers():
    response = _count_document(
        candidates=Int64(3),
        eligible=Int64(2),
        excluded=Int64(1),
        reliable=Int64(2),
        in_period=Int64(1),
    )
    fixture, _collection, repository, read, plan = await _direct_count_fixture(response)

    result = await repository.count_date_cohort(fixture["principal"], read, plan=plan)

    assert result == DateCohortCounts(
        candidates=3,
        eligible=2,
        excluded=1,
        reliable=2,
        in_period=1,
        policy_version=7,
    )
    assert type(result.in_period) is int


@pytest.mark.asyncio
async def test_source_failure_is_safe_and_never_becomes_zero():
    fixture, _binding, config, collection, repository = cohort_fixture()
    collection.aggregate_error = RuntimeError("credential mongodb://secret.invalid")
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry((synthetic_definition(),)),
    )

    with pytest.raises(SourceUnavailable) as error:
        await service.evaluate(fixture["principal"], metric_request())

    assert str(error.value) == "Metric is unavailable."
    assert "secret" not in str(error.value)
    assert len(collection.calls) == 1


@pytest.mark.asyncio
async def test_final_policy_revocation_blocks_metric_release_after_query():
    def revoke_on_final_authorize(state, _principal):
        if state["load_count"] >= 5:
            current = state["snapshot"]
            state["snapshot"] = current.model_copy(
                update={
                    "user": current.user.model_copy(
                        update={"approval_status": "revoked"}
                    )
                }
            )
        return state["snapshot"]

    fixture, _binding, config, collection, repository = cohort_fixture(
        [document(1, "virtualyard", PERIOD_START)], loader=revoke_on_final_authorize
    )
    service = service_for(
        fixture,
        repository,
        config,
        registry=MetricRegistry(
            (synthetic_definition(eligible={"virtualyard"}, excluded=()),)
        ),
    )

    with pytest.raises(AccessDenied):
        await service.evaluate(fixture["principal"], metric_request())

    assert len(collection.calls) == 1
    assert fixture["state"]["load_count"] >= 5


@pytest.mark.parametrize("escape", ["out", "nested_facet", "lookup", "function"])
def test_metric_facet_validator_rejects_nested_query_and_write_escapes(escape):
    binding, _config = build_lead_adapter(site_settings())
    good = [
        {"$match": {"dealer": {"$in": [SITE_A_VALUE]}}},
        {"$project": {"_id": 1, "platform": 1, "leadCreatedDate": 1}},
        {
            "$facet": {
                "candidates": [{"$count": "count"}],
                "eligible": [
                    {"$match": {"$expr": {"$eq": ["$platform", "virtualyard"]}}},
                    {"$count": "count"},
                ],
                "excluded": [{"$count": "count"}],
                "reliable": [{"$count": "count"}],
                "in_period": [{"$count": "count"}],
            }
        },
    ]
    bad = deepcopy(good)
    if escape == "out":
        bad[2]["$facet"]["eligible"].insert(0, {"$out": "leads_copy"})
    elif escape == "nested_facet":
        bad[2]["$facet"]["eligible"].insert(0, {"$facet": {"x": [{"$count": "count"}]}})
    elif escape == "lookup":
        bad[2]["$facet"]["eligible"].insert(0, {"$lookup": {"from": "users"}})
    else:
        bad[2]["$facet"]["eligible"][0]["$match"]["$expr"] = {
            "$eq": [{"$function": {"body": "return true"}}, True]
        }

    with pytest.raises(ValueError):
        _validate_pipeline(bad, binding, ())


def test_user_request_types_do_not_accept_raw_metric_pipelines():
    with pytest.raises(ValidationError):
        MetricRequest.model_validate(
            {
                "metric_id": "total_leads",
                "site": "site_a",
                "pipeline": [{"$out": "copy"}],
            }
        )


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from walk(child)


def run(awaitable):
    """Sync helper for one test that is intentionally not async."""
    import asyncio

    return asyncio.run(awaitable)
