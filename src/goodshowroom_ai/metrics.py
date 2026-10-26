"""Code-owned Q-01 definitions and deterministic internal metric evidence.

Results still require disclosure policy and ResponseBuilder checks (006/007).
No SDK tool, HTTP route, chart or narrative calculation is registered here.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    Contract,
    DataAccess,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.normalization import LeadAdapterConfig, Platform, PlatformDateRule
from goodshowroom_ai.operational import OperationalReadRepository, SourceUnavailable
from goodshowroom_ai.operational_models import (
    DateCohortCounts,
    DateCohortPlan,
    DateFilter,
    ReadRequest,
    Q01_TOOL,
)
from goodshowroom_ai.source_dates import (
    DateUnavailable,
    ReportingPeriod,
    reporting_period,
)
from goodshowroom_ai.storage.models import utc_datetime


class MetricDefinition(Contract):
    """Trusted implementation configuration; never an admin/prompt payload."""

    metric_id: Literal["total_leads", "converted_leads"]
    version: int = Field(default=1, ge=1)
    meaning: str = Field(min_length=1, max_length=1000)
    enabled: bool = False
    question_id: Literal["Q-01", "deferred"] = "Q-01"
    database: Literal["byd-leads-new"] = "byd-leads-new"
    collection: Literal["leads"] = "leads"
    source_identity_field: Literal["_id"] = "_id"
    date_field: Literal["leadCreatedDate"] = "leadCreatedDate"
    platform_field: Literal["platform"] = "platform"
    identity: Literal["unreviewed", "distinct_source_records"] = "unreviewed"
    identity_review: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    exclusions_review: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$"
    )
    eligible_platforms: frozenset[Platform] = frozenset(
        {"virtualyard", "dealer_studio", "autogate", "manual", "sms"}
    )
    excluded_platforms: frozenset[Platform] = frozenset()
    date_rules: tuple[PlatformDateRule, ...] = ()
    date_representation: Literal["bson_date"] = "bson_date"
    timezone_rule: Literal["configured_single_site"] = "configured_single_site"
    filters: tuple[Literal["site"], ...] = ("site",)
    aggregation: Literal["full_authorized_cohort_count"] = (
        "full_authorized_cohort_count"
    )
    coverage_rule: Literal["unknowns_prevent_exact_total"] = (
        "unknowns_prevent_exact_total"
    )
    authorization_rule: Literal["current_tool_metric_record_field_grants"] = (
        "current_tool_metric_record_field_grants"
    )

    @model_validator(mode="after")
    def consistent(self):
        if self.metric_id == "converted_leads" and self.enabled:
            raise ValueError("Conversion is outside the approved question set")
        if (
            not self.eligible_platforms
            or self.eligible_platforms & self.excluded_platforms
        ):
            raise ValueError("Invalid source scope")
        if len({r.platform for r in self.date_rules}) != len(self.date_rules):
            raise ValueError("Ambiguous date provenance")
        if any(r.platform not in self.eligible_platforms for r in self.date_rules):
            raise ValueError("Date rule outside eligible source scope")
        return self

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self.metric_id == "total_leads"
            and self.question_id == "Q-01"
            and self.identity == "distinct_source_records"
            and self.identity_review
            and self.exclusions_review
        )


class MetricRegistry:
    """Versioned exact entries; no fallback to a model interpretation."""

    def __init__(self, definitions: tuple[MetricDefinition, ...] | None = None):
        entries = (
            definitions
            if definitions is not None
            else (
                MetricDefinition(
                    metric_id="total_leads",
                    enabled=True,
                    meaning="Eligible canonical leads received in the previous seven completed dealership days; live identity, exclusions and provenance remain unreviewed.",
                ),
                MetricDefinition(
                    metric_id="converted_leads",
                    question_id="deferred",
                    meaning="Requires an approved conversion event, source relationships and deduplication rule.",
                ),
            )
        )
        if len({d.metric_id for d in entries}) != len(entries):
            raise ValueError("Duplicate metric definition")
        self._entries = tuple(entries)

    def get(self, metric_id: str) -> MetricDefinition | None:
        return next((d for d in self._entries if d.metric_id == metric_id), None)


class MetricRequest(Contract):
    metric_id: str = Field(default="total_leads", pattern=r"^[a-z][a-z0-9_]{0,99}$")
    question_id: Literal["Q-01", "Q-02", "Q-03", "Q-04", "Q-05"] = "Q-01"
    site: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class MetricCoverage(Contract):
    scope: Literal["full_authorized_query"] = "full_authorized_query"
    candidates: int = Field(ge=0)
    eligible: int = Field(ge=0)
    excluded: int = Field(ge=0)
    unknown_eligibility: int = Field(ge=0)
    reliable_dates: int = Field(ge=0)
    unknown_dates: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.eligible + self.excluded + self.unknown_eligibility != self.candidates
            or self.reliable_dates + self.unknown_dates != self.eligible
        ):
            raise ValueError("Inconsistent metric coverage")
        return self


class MetricResult(Contract):
    """Internal evidence only; partial verified_count is never an exact total."""

    metric_id: str
    metric_version: int | None = Field(ge=1)
    site: str
    status: Literal["complete", "partial", "unsupported", "unavailable"]
    value: int | None = Field(default=None, ge=0)
    verified_count: int | None = Field(default=None, ge=0)
    period: ReportingPeriod | None = None
    coverage: MetricCoverage | None = None
    policy_version: int = Field(ge=0)
    retrieved_at: datetime
    source: Literal["byd-leads-new.leads"] = "byd-leads-new.leads"
    warnings: tuple[str, ...] = ()

    @field_validator("retrieved_at")
    @classmethod
    def utc(cls, value):
        return utc_datetime(value)

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "complete":
            if (
                self.value is None
                or self.verified_count != self.value
                or self.coverage is None
                or self.period is None
                or self.coverage.unknown_eligibility
                or self.coverage.unknown_dates
            ):
                raise ValueError("An exact count requires complete evidence")
        elif self.value is not None:
            raise ValueError("Uncertain results cannot contain an exact total")
        if (
            self.status in {"unsupported", "unavailable"}
            and self.verified_count is not None
        ):
            raise ValueError("Unavailable results cannot contain verified counts")
        if self.status == "partial" and (
            self.coverage is None or self.period is None or self.verified_count is None
        ):
            raise ValueError("Partial results require identifiable verified evidence")
        if self.verified_count is not None and (
            self.coverage is None or self.verified_count > self.coverage.reliable_dates
        ):
            raise ValueError("Verified count exceeds reliable coverage")
        return self


class MetricService:
    def __init__(
        self,
        *,
        repository: OperationalReadRepository,
        authorization: AuthorizationService,
        config: LeadAdapterConfig,
        clock: Callable[[], datetime],
        registry: MetricRegistry | None = None,
    ):
        self._repository, self._authorization, self._config, self._clock = (
            repository,
            authorization,
            config,
            clock,
        )
        self._registry = registry if registry is not None else MetricRegistry()

    async def evaluate(
        self, principal: Principal, request: MetricRequest
    ) -> MetricResult:
        try:
            if not isinstance(request, MetricRequest):
                raise ValueError("Typed metric request required")
            definition = self._registry.get(request.metric_id)
            access = DataAccess(
                database="byd-leads-new",
                collection="leads",
                site=request.site,
                fields=self._config.required_fields,
                tool=Q01_TOOL,
            )
            admission = await self._authorization.authorize(
                principal, ProtectedAction(action="tool", data=(access,))
            )
            warning = None
            if request.question_id != "Q-01":
                warning = "question_family_not_enabled"
            elif definition is None or not definition.enabled:
                warning = "metric_not_enabled"
            elif not definition.ready:
                warning = "identity_or_exclusions_unreviewed"
            if warning:
                await self._authorization.recheck(admission)
                return MetricResult(
                    metric_id=request.metric_id,
                    site=request.site,
                    metric_version=definition.version if definition else None,
                    status="unsupported",
                    policy_version=admission.policy_version,
                    retrieved_at=utc_datetime(self._clock()),
                    warnings=(warning,),
                )
            protected = access.model_copy(update={"metric": "total_leads"})
            decision = await self._authorization.authorize(
                principal, ProtectedAction(action="tool", data=(protected,))
            )
            if decision.snapshot != admission.snapshot:
                raise AccessDenied("Access is not allowed.")
            try:
                period = reporting_period(
                    settings=decision.snapshot.sites,
                    site=request.site,
                    now=self._clock(),
                    kind="last_seven_days",
                )
            except DateUnavailable:
                await self._authorization.recheck(decision)
                return MetricResult(
                    metric_id="total_leads",
                    site=request.site,
                    metric_version=definition.version,
                    status="unavailable",
                    policy_version=decision.policy_version,
                    retrieved_at=utc_datetime(self._clock()),
                    warnings=("site_timezone_unavailable",),
                )
            reliable = frozenset(
                r.platform
                for r in definition.date_rules
                if r.rule.provenance in {"original", "native"}
                and r.rule.review_reference
            )
            plan = DateCohortPlan(
                platform_field=self._config.platform_column,
                date_field=self._config.date_column,
                eligible_platforms=definition.eligible_platforms,
                excluded_platforms=definition.excluded_platforms,
                reliable_platforms=reliable,
                period=DateFilter(
                    field=self._config.date_column,
                    start=period.start_utc,
                    end=period.end_utc,
                ),
            )
            read = ReadRequest(
                binding=self._config.binding,
                site=request.site,
                tool=Q01_TOOL,
                fields=self._config.required_fields,
                metric="total_leads",
            )
            counts = await self._repository.count_date_cohort(
                principal, read, plan=plan
            )
            if (
                not isinstance(counts, DateCohortCounts)
                or counts.policy_version != decision.policy_version
            ):
                raise AccessDenied("Access is not allowed.")
            await self._authorization.recheck(decision)
            unknown_source = counts.candidates - counts.eligible - counts.excluded
            unknown_date = counts.eligible - counts.reliable
            warnings = tuple(
                code
                for condition, code in (
                    (unknown_source, "unknown_source_eligibility"),
                    (unknown_date, "received_date_coverage_incomplete"),
                )
                if condition
            )
            status = (
                "complete"
                if not warnings
                else "partial"
                if counts.reliable
                else "unavailable"
            )
            coverage = MetricCoverage(
                candidates=counts.candidates,
                eligible=counts.eligible,
                excluded=counts.excluded,
                unknown_eligibility=unknown_source,
                reliable_dates=counts.reliable,
                unknown_dates=unknown_date,
            )
            return MetricResult(
                metric_id="total_leads",
                site=request.site,
                metric_version=definition.version,
                status=status,
                value=counts.in_period if status == "complete" else None,
                verified_count=counts.in_period if status != "unavailable" else None,
                period=period,
                coverage=coverage,
                policy_version=decision.policy_version,
                retrieved_at=utc_datetime(self._clock()),
                warnings=warnings,
            )
        except AccessDenied:
            raise
        except Exception:
            raise SourceUnavailable("Metric is unavailable.") from None
