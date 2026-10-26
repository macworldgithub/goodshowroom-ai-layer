"""Deterministic Q-01 responses from disclosure-approved evidence only.

Internal contracts: no HTTP route, model call, streaming, or persistence. Callers
must resolve the owned chat before building its server-only ResponseContext, then
recheck access again at eventual commit/release. Never pass raw MetricResult here.
"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    Contract,
    DataAccess,
    Principal,
    ProtectedAction,
)
from goodshowroom_ai.disclosure import DisclosureResult, DisclosureService
from goodshowroom_ai.execution import MAX_WIRE_BYTES
from goodshowroom_ai.metrics import MetricRequest
from goodshowroom_ai.normalization import LeadAdapterConfig
from goodshowroom_ai.operational_models import Q01_TOOL
from goodshowroom_ai.storage.models import utc_datetime

MAX_RESPONSE_BYTES = MAX_WIRE_BYTES
Notice = Literal["partial", "unavailable", "unsupported", "clarification", "withheld"]
_NOTICES = {
    "partial": ("partial", "The available data is incomplete.", "incomplete_data"),
    "unavailable": ("unavailable", "The required data is unavailable.", "missing_data"),
    "unsupported": (
        "unsupported",
        "This question is not supported yet.",
        "question_not_supported",
    ),
    "clarification": (
        "clarification",
        "Which permitted dealership should I use?",
        "scope_required",
    ),
    "withheld": (
        "unavailable",
        "This result cannot be shown under the current disclosure policy.",
        "disclosure_withheld",
    ),
}


class ResponseInvalid(ValueError):
    """Safe error category for the later transport/orchestrator adapter."""


class ResponseContext(Contract):
    """Trusted resolved chat binding, never a browser/model request body."""

    request_id: UUID
    session_id: UUID
    owner_id: UUID
    mode: Literal["saved", "temporary"]
    chat_login_id: UUID | None = None


class AnswerPeriod(Contract):
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime
    timezone: str = Field(min_length=1, max_length=100)
    label: Literal["Previous seven completed dealership calendar days"] = (
        "Previous seven completed dealership calendar days"
    )

    @field_validator("start_utc", "end_utc")
    @classmethod
    def utc(cls, value):
        return utc_datetime(value)

    @model_validator(mode="after")
    def boundaries(self):
        try:
            zone = ZoneInfo(self.timezone)
        except (KeyError, ValueError):
            raise ValueError("Unknown reporting timezone") from None
        if self.end_utc <= self.start_utc or any(
            local.isoformat() != instant.astimezone(zone).isoformat()
            for local, instant in (
                (self.start_local, self.start_utc),
                (self.end_local, self.end_utc),
            )
        ):
            raise ValueError("Inconsistent reporting period")
        if self.end_local.date() - self.start_local.date() != timedelta(days=7) or any(
            (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0)
            for local in (self.start_local, self.end_local)
        ):
            raise ValueError("Expected seven completed calendar days")
        return self


class AnswerScope(Contract):
    site: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    record_scope: Literal["full_permitted_site"] = "full_permitted_site"


class Fact(Contract):
    fact_id: Literal["fact_total_leads"] = "fact_total_leads"
    metric_id: Literal["total_leads"] = "total_leads"
    metric_version: int = Field(ge=1)
    value: int = Field(ge=0)
    unit: Literal["leads"] = "leads"
    evidence_ids: tuple[Literal["evidence_total_leads"]] = ("evidence_total_leads",)


class Claim(Contract):
    text: str = Field(max_length=200)
    fact_ids: tuple[Literal["fact_total_leads"]] = ("fact_total_leads",)


class Source(Contract):
    source_id: Literal["source_leads"] = "source_leads"
    label: Literal["Lead records"] = "Lead records"


class Evidence(Contract):
    evidence_id: Literal["evidence_total_leads"] = "evidence_total_leads"
    source_id: Literal["source_leads"] = "source_leads"
    metric_id: Literal["total_leads"] = "total_leads"
    metric_version: int = Field(ge=1)
    provenance: Literal["reviewed_metric_definition"] = "reviewed_metric_definition"


class Column(Contract):
    key: Literal["label", "value"]
    label: Literal["Metric", "Count"]
    type: Literal["string", "number"]
    unit: Literal["leads"] | None = None


class MetricRow(Contract):
    label: Literal["Total leads"] = "Total leads"
    value: int = Field(ge=0)
    fact_id: Literal["fact_total_leads"] = "fact_total_leads"


class Dataset(Contract):
    dataset_id: Literal["dataset_total_leads"] = "dataset_total_leads"
    columns: tuple[Column, Column] = (
        Column(key="label", label="Metric", type="string"),
        Column(key="value", label="Count", type="number", unit="leads"),
    )
    rows: tuple[MetricRow]
    next_cursor: None = None


class Bindings(Contract):
    label: Literal["label"] = "label"
    value: Literal["value"] = "value"


class Visual(Contract):
    kind: Literal["card", "table"]
    dataset_id: Literal["dataset_total_leads"] = "dataset_total_leads"
    field_bindings: Bindings = Bindings()


class Coverage(Contract):
    status: Literal["complete", "partial", "unknown", "suppressed"]
    # Internal cohort counts were not cleared for release by TICKET-006.
    counts_disclosed: Literal[False] = False


class StructuredResponse(Contract):
    """Bounded first-slice API response, with a closed evidence graph.

    Rendering is TICKET-040. Additional metrics/charts/record rows need their own
    typed extension; arbitrary model JSON/configuration is never accepted.
    """

    status: Literal[
        "complete", "partial", "clarification", "unsupported", "unavailable"
    ]
    answer: str = Field(min_length=1, max_length=1000)
    claims: tuple[Claim, ...] = Field(default=(), max_length=1)
    facts: tuple[Fact, ...] = Field(default=(), max_length=1)
    datasets: tuple[Dataset, ...] = Field(default=(), max_length=1)
    visuals: tuple[Visual, ...] = Field(default=(), max_length=2)
    sources: tuple[Source, ...] = Field(default=(), max_length=1)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=1)
    coverage: Coverage
    warnings: tuple[
        Literal[
            "incomplete_data",
            "missing_data",
            "question_not_supported",
            "scope_required",
            "disclosure_withheld",
        ],
        ...,
    ] = Field(default=(), max_length=1)
    period: AnswerPeriod | None = None
    scope: AnswerScope | None = None
    retrieved_at: datetime | None = None
    policy_version: int = Field(ge=0)

    @field_validator("retrieved_at")
    @classmethod
    def utc(cls, value):
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def grounded(self):
        if self.status == "complete":
            if len(self.facts) != 1:
                raise ValueError("Complete response requires verified facts")
            fact = self.facts[0]
            answer = f"Total leads: {fact.value}."
            if (
                self.answer != answer
                or self.claims != (Claim(text=answer),)
                or self.datasets != (Dataset(rows=(MetricRow(value=fact.value),)),)
                or self.visuals != (Visual(kind="card"), Visual(kind="table"))
                or self.sources != (Source(),)
                or self.evidence != (Evidence(metric_version=fact.metric_version),)
                or self.coverage != Coverage(status="complete")
                or self.warnings
                or self.period is None
                or self.scope is None
                or self.retrieved_at is None
                or self.retrieved_at < self.period.end_utc
            ):
                raise ValueError("Response facts, claims and visuals disagree")
        else:
            permitted = any(
                self.status == status
                and self.answer == text
                and self.warnings == (code,)
                for status, text, code in _NOTICES.values()
            )
            coverage = (
                "suppressed"
                if self.warnings == ("disclosure_withheld",)
                else "partial"
                if self.status == "partial"
                else "unknown"
            )
            if (
                not permitted
                or any(
                    (
                        self.facts,
                        self.claims,
                        self.datasets,
                        self.visuals,
                        self.sources,
                        self.evidence,
                    )
                )
                or self.period is not None
                or self.scope is not None
                or self.retrieved_at is not None
                or self.coverage != Coverage(status=coverage)
            ):
                raise ValueError("Notice cannot disclose business evidence")
        return self


class BuiltResponse(Contract):
    request_id: UUID
    response: StructuredResponse


def _notice(kind: Notice, policy_version: int) -> StructuredResponse:
    status, answer, warning = _NOTICES[kind]
    coverage = (
        "suppressed"
        if kind == "withheld"
        else ("partial" if kind == "partial" else "unknown")
    )
    return StructuredResponse(
        status=status,
        answer=answer,
        warnings=(warning,),
        coverage=Coverage(status=coverage),
        policy_version=policy_version,
    )


class ResponseBuilder:
    def __init__(
        self,
        *,
        disclosure: DisclosureService,
        authorization: AuthorizationService,
        config: LeadAdapterConfig,
    ):
        self._disclosure, self._authorization, self._config = (
            disclosure,
            authorization,
            config,
        )

    async def build_metric(
        self,
        principal: Principal,
        context: ResponseContext,
        request: MetricRequest,
        *,
        candidate_answer: str | None = None,
    ) -> BuiltResponse:
        # All free model prose is rebuilt. Matching a few digits cannot verify
        # relationships, implied arithmetic, word numbers, or injected markup.
        if not isinstance(request, MetricRequest):
            raise AccessDenied("Access is not allowed.")
        action = self._action(context, request)
        decision = await self._authorization.authorize(principal, action)
        result = await self._disclosure.evaluate(principal, request)
        try:
            if not isinstance(result, DisclosureResult):
                raise ValueError()
            result = DisclosureResult.model_validate_json(
                result.model_dump_json(warnings="error")
            )
            response = _notice("withheld", decision.policy_version)
            if result.aggregate is not None:
                item = result.aggregate
                if (
                    request.question_id != "Q-01"
                    or item.metric_id != request.metric_id
                    or item.site != request.site
                    or item.period.site != request.site
                    or item.period.kind != "last_seven_days"
                    or item.policy_version != decision.policy_version
                    or not decision.constraints
                    or any(
                        c.record_mode != "all_permitted"
                        for group in decision.constraints
                        for c in group
                    )
                ):
                    raise AccessDenied("Access is not allowed.")
                period = AnswerPeriod(
                    **{
                        key: getattr(item.period, key)
                        for key in (
                            "start_local",
                            "end_local",
                            "start_utc",
                            "end_utc",
                            "timezone",
                        )
                    }
                )
                response = StructuredResponse(
                    status="complete",
                    answer=f"Total leads: {item.value}.",
                    claims=(Claim(text=f"Total leads: {item.value}."),),
                    facts=(Fact(value=item.value, metric_version=item.metric_version),),
                    datasets=(Dataset(rows=(MetricRow(value=item.value),)),),
                    visuals=(Visual(kind="card"), Visual(kind="table")),
                    sources=(Source(),),
                    evidence=(Evidence(metric_version=item.metric_version),),
                    coverage=Coverage(status="complete"),
                    period=period,
                    scope=AnswerScope(site=item.site),
                    retrieved_at=item.retrieved_at,
                    policy_version=item.policy_version,
                )
            built = self._validated(context, response)
        except AccessDenied:
            raise
        except Exception:
            raise ResponseInvalid("Response is invalid.") from None
        await self._authorization.recheck(decision)
        return built

    async def build_notice(
        self,
        principal: Principal,
        context: ResponseContext,
        kind: Notice,
    ) -> BuiltResponse:
        """Trusted orchestrator selects a content-free outcome, never the model.

        AccessDenied and SourceUnavailable stay errors for the later API adapter;
        do not turn exceptions into successful missing-data/zero answers.
        """
        decision = await self._authorization.authorize(principal, self._action(context))
        try:
            built = self._validated(context, _notice(kind, decision.policy_version))
        except Exception:
            raise ResponseInvalid("Response is invalid.") from None
        await self._authorization.recheck(decision)
        return built

    def _action(self, context: ResponseContext, request: MetricRequest | None = None):
        if not isinstance(context, ResponseContext) or (
            request is not None and not isinstance(request, MetricRequest)
        ):
            raise AccessDenied("Access is not allowed.")
        data = (
            ()
            if request is None
            else (
                DataAccess(
                    database="byd-leads-new",
                    collection="leads",
                    site=request.site,
                    fields=self._config.required_fields,
                    tool=Q01_TOOL,
                    metric=request.metric_id,
                ),
            )
        )
        return ProtectedAction(
            action="chat" if request is None else "tool",
            owner_id=context.owner_id,
            mode=context.mode,
            chat_login_id=context.chat_login_id,
            data=data,
        )

    @staticmethod
    def _validated(
        context: ResponseContext, response: StructuredResponse
    ) -> BuiltResponse:
        result = BuiltResponse(request_id=context.request_id, response=response)
        encoded = result.model_dump_json(warnings="error")
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ResponseInvalid("Response is invalid.")
        # JSON revalidation also catches accidental model_copy/model_construct
        # bypasses in internal callers before a response can leave this module.
        return BuiltResponse.model_validate_json(encoded)
