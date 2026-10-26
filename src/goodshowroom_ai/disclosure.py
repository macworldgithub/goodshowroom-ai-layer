"""Fail-closed aggregate release boundary for internal backend consumers.

Live thresholds/differencing rules are not approved. Only an explicitly reviewed,
fixed publication can be released; a threshold alone never enables live totals.
No query history or temporary conversation content is stored here.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    Contract,
    DataAccess,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.metrics import MetricRequest, MetricResult, MetricService
from goodshowroom_ai.normalization import LeadAdapterConfig
from goodshowroom_ai.operational_models import Q01_TOOL
from goodshowroom_ai.source_dates import ReportingPeriod


class ReviewedPublication(Contract):
    """Trusted code configuration, never an HTTP/model/admin payload.

    Review must cover the full release family, external/complementary totals,
    prior publications, and access population. One immutable cell per metric/site
    is supported. Changing it requires a fresh disclosure review across versions.
    The exact evidence (including retrieval time) is pinned, so source changes or
    rolling date windows cannot become a sequence of released live counts.
    """

    review_reference: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    minimum_group: int = Field(ge=1)
    evidence: MetricResult = Field(repr=False)

    @model_validator(mode="after")
    def safe_cell(self):
        evidence = self.evidence
        if (
            evidence.metric_id != "total_leads"
            or evidence.status != "complete"
            or evidence.value is None
            or evidence.value < self.minimum_group
            or evidence.warnings
            or evidence.metric_version is None
            or evidence.period is None
            or evidence.period.site != evidence.site
            or evidence.period.kind != "last_seven_days"
        ):
            raise ValueError("Publication requires complete reviewed evidence")
        return self


class ReleasedAggregate(Contract):
    metric_id: Literal["total_leads"] = "total_leads"
    value: int = Field(ge=1)
    site: str
    metric_version: int = Field(ge=1)
    policy_version: int = Field(ge=0)
    # Copy the validated period and retrieval time, without coverage counters.
    period: ReportingPeriod
    retrieved_at: datetime
    source: Literal["byd-leads-new.leads"] = "byd-leads-new.leads"


class DisclosureResult(Contract):
    """Single permitted dataset for text, tables, and future chart consumers.

    Withholding exposes no count, coverage, threshold or internal denial reason.
    This is an internal contract, not a registered route or ResponseBuilder.
    """

    status: Literal["withheld", "released"] = "withheld"
    aggregate: ReleasedAggregate | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "released") != (self.aggregate is not None):
            raise ValueError("Inconsistent disclosure output")
        return self


class DisclosureService:
    """Authorize -> calculate -> suppress/project -> recheck before return.

    No breakdown, export, user-selected filter, alternate group, partial count,
    or live release channel exists. Safe fallback is generic withholding. No
    broader total is fetched automatically: it could expose a suppressed cell.
    """

    def __init__(
        self,
        *,
        metrics: MetricService,
        authorization: AuthorizationService,
        config: LeadAdapterConfig,
        publications: tuple[ReviewedPublication, ...] = (),
    ):
        keys = [(p.evidence.metric_id, p.evidence.site) for p in publications]
        if len(keys) != len(set(keys)):
            raise ValueError("Only one reviewed cell per metric and site is allowed")
        self._metrics = metrics
        self._authorization = authorization
        self._config = config
        self._publications = publications

    async def evaluate(
        self, principal: Principal, request: MetricRequest
    ) -> DisclosureResult:
        if not isinstance(request, MetricRequest):
            raise AccessDenied("Access is not allowed.")
        decision = await self._authorization.authorize(
            principal,
            ProtectedAction(
                action="tool",
                data=(
                    DataAccess(
                        database="byd-leads-new",
                        collection="leads",
                        site=request.site,
                        fields=self._config.required_fields,
                        tool=Q01_TOOL,
                        metric=request.metric_id,
                    ),
                ),
            ),
        )
        publication = next(
            (
                p
                for p in self._publications
                if p.evidence.metric_id == request.metric_id
                and p.evidence.site == request.site
            ),
            None,
        )
        result = DisclosureResult()
        # Prevent narrow/overlapping assignment and selected-record cohorts from
        # being interpreted as the reviewed full-site publication.
        full_site = all(
            clause.record_mode == "all_permitted"
            for group in decision.constraints
            for clause in group
        )
        if publication is not None and full_site and request.question_id == "Q-01":
            evidence = await self._metrics.evaluate(principal, request)
            if (
                evidence == publication.evidence
                and evidence.policy_version == decision.policy_version
            ):
                result = DisclosureResult(
                    status="released",
                    aggregate=ReleasedAggregate(
                        value=evidence.value,
                        site=evidence.site,
                        metric_version=evidence.metric_version,
                        policy_version=evidence.policy_version,
                        period=evidence.period,
                        retrieved_at=evidence.retrieved_at,
                    ),
                )
        await self._authorization.recheck(decision)
        return result
