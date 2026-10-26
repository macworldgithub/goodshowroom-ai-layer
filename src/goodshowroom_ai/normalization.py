"""Normalize already-authorized source rows without inventing business meaning."""

from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field, model_validator

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    Contract,
    DataAccess,
    Principal,
    ProtectedAction,
    RecordAttributes,
)
from goodshowroom_ai.operational import OperationalReadRepository, SourceUnavailable
from goodshowroom_ai.operational_models import (
    CollectionBinding,
    FieldBinding,
    PagePosition,
    ReadPage,
    ReadRequest,
    ReadRow,
    SiteBinding,
)
from goodshowroom_ai.source_dates import DateEvidence, DateRule, normalize_date
from goodshowroom_ai.storage.identities import SourceReference
from goodshowroom_ai.storage.settings import AppSetting, IdentityMapping
from goodshowroom_ai.storage.models import utc_datetime

Platform = Literal["virtualyard", "dealer_studio", "autogate", "manual", "sms"]
_PLATFORMS = {"virtualyard", "dealer_studio", "autogate", "manual", "sms"}
_STATUS = {
    "new": "new",
    "committed": "committed",
    "qualification": "qualification",
    "sold": "sold",
    "lost": "lost",
    "opted out": "opted_out",
}
_STAGES = {
    "NEW ENQUIRIES": "new_enquiries",
    "IN FOLLOW-UP": "in_follow_up",
    "AI QUALIFYING": "ai_qualifying",
    "TEST DRIVE BOOKED": "test_drive_booked",
    "DELIVERED": "delivered",
    "LOST": "lost",
    "OPTED OUT": "opted_out",
}


class IdentityColumn(Contract):
    column: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$")
    key_field: Literal[
        "_id", "id", "leadId", "virtualyardId", "autogateLeadId", "customerId"
    ]
    key_type: Literal["string", "objectid"]


class StaffColumn(Contract):
    """No default name-to-ID mapping. Configure only reviewed stable-ID fields."""

    column: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$")
    collection: Literal["users", "staff_site_memberships"]
    key_field: Literal["_id", "id", "user_id", "staff_id"]
    key_type: Literal["string", "objectid"]
    review_reference: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class PlatformDateRule(Contract):
    platform: Platform
    rule: DateRule


class LeadAdapterConfig(Contract):
    """Internal mapping to canonical ReadRow columns; never admin/model input."""

    binding: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    platform_column: str = "platform"
    date_column: str = "leadCreatedDate"
    status_column: str = "status"
    stage_column: str = "stage"
    site_columns: tuple[str, ...] = Field(min_length=1, max_length=10)
    identities: tuple[IdentityColumn, ...] = Field(default=(), max_length=10)
    staff: tuple[StaffColumn, ...] = Field(default=(), max_length=10)
    date_rules: tuple[PlatformDateRule, ...] = Field(default=(), max_length=5)

    @property
    def required_fields(self) -> frozenset[str]:
        return frozenset(
            (
                self.platform_column,
                self.date_column,
                self.status_column,
                self.stage_column,
                *self.site_columns,
                *(s.column for s in self.staff),
                *(i.column for i in self.identities),
            )
        )

    @model_validator(mode="after")
    def unambiguous(self):
        if (
            len({r.platform for r in self.date_rules}) != len(self.date_rules)
            or len(set(self.site_columns)) != len(self.site_columns)
            or len({s.column for s in self.staff}) != len(self.staff)
            or len({i.column for i in self.identities}) != len(self.identities)
        ):
            raise ValueError("Ambiguous normalization mapping")
        return self


class SiteResolution(Contract):
    site: str | None = None
    state: Literal["verified", "missing", "unknown", "ambiguous"]


def build_lead_adapter(
    settings: AppSetting,
) -> tuple[CollectionBinding, LeadAdapterConfig]:
    """Observed read schema plus explicit site aliases; no provenance attestation.

    This builds a proposal for trusted startup, not a live registration.
    No rules here prove original timestamps, ownership or deduplication.
    """
    if (
        not isinstance(settings, AppSetting)
        or settings.id != "sites"
        or not settings.sites
    ):
        raise SourceUnavailable("Source is unavailable.")
    config = LeadAdapterConfig(
        binding="lead_records",
        site_columns=("dealer", "dealerName"),
        identities=tuple(
            IdentityColumn(column=key, key_field=key, key_type="string")
            for key in ("leadId", "virtualyardId", "autogateLeadId", "customerId")
        ),
    )
    fields = [FieldBinding(name="record_id", path="_id", kind="objectid")]
    fields.extend(
        FieldBinding(
            name=key,
            path=key,
            kind="source_date" if key == config.date_column else "string",
        )
        for key in sorted(config.required_fields)
    )
    try:
        binding = CollectionBinding(
            name=config.binding,
            database="byd-leads-new",
            collection="leads",
            fields=tuple(fields),
            identity="record_id",
            site_field="dealer",
            sites=tuple(
                SiteBinding(site=s.site_id, values=(s.site_id, *s.aliases))
                for s in settings.sites
            ),
        )
    except ValueError:
        raise SourceUnavailable("Source is unavailable.") from None
    return binding, config


def resolve_site(values: tuple, settings: AppSetting | None) -> SiteResolution:
    """Exact configured aliases only. Never infer All Sites or fuzzy ownership."""
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return SiteResolution(state="missing")
    if (
        settings is None
        or settings.id != "sites"
        or any(type(v) is not str for v in present)
    ):
        return SiteResolution(state="unknown")
    aliases = {
        alias.strip().casefold(): s.site_id
        for s in settings.sites
        for alias in (s.site_id, *s.aliases)
    }
    found = set()
    for value in present:
        key = value.strip().casefold()
        if key in {"all", "all sites", "all locations", "*"} or key not in aliases:
            return SiteResolution(state="unknown")
        found.add(aliases[key])
    return (
        SiteResolution(site=found.pop(), state="verified")
        if len(found) == 1
        else SiteResolution(state="ambiguous")
    )


def source_reference(
    *, database, collection, key_field, key_type, value
) -> SourceReference:
    """Preserve declared type: a 24-character string is not implicitly an ObjectId."""
    if key_field not in {
        "_id",
        "id",
        "leadId",
        "virtualyardId",
        "autogateLeadId",
        "customerId",
        "customer_id",
        "opportunity_id",
        "sales_log_id",
        "user_id",
        "staff_id",
    }:
        raise ValueError("Stable source identifier required")
    if key_type == "string" and type(value) is not str:
        raise ValueError("Invalid source identifier")
    if key_type == "objectid":
        if isinstance(value, ObjectId):
            value = str(value)
        elif type(value) is not str:
            raise ValueError("Invalid source identifier")
    if type(value) is not str or not value.strip():
        raise ValueError("Missing source identifier")
    return SourceReference(
        database=database,
        collection=collection,
        key_field=key_field,
        key_type=key_type,
        key_value=value,
    )


class NormalizedLead(Contract):
    reference: SourceReference
    platform: Platform | None
    status: str | None
    stage: str | None
    site: str
    date: DateEvidence
    aliases: tuple[SourceReference, ...] = ()
    assigned_to: tuple[SourceReference, ...] = ()
    warnings: tuple[str, ...] = ()


def _normalize_lead(
    row: ReadRow,
    *,
    config: LeadAdapterConfig,
    settings: AppSetting | None,
    mappings: tuple[IdentityMapping, ...],
    expected_site: str,
    now: datetime,
) -> NormalizedLead:
    if (row.reference.database, row.reference.collection) != ("byd-leads-new", "leads"):
        raise SourceUnavailable("Source is unavailable.")
    site = resolve_site(tuple(row.values.get(c) for c in config.site_columns), settings)
    if site.state != "verified" or site.site != expected_site:
        raise SourceUnavailable("Source is unavailable.")
    raw_platform = row.values.get(config.platform_column)
    platform = raw_platform.strip().casefold() if type(raw_platform) is str else None
    platform = platform if platform in _PLATFORMS else None
    rule = next(
        (r.rule for r in config.date_rules if r.platform == platform), DateRule()
    )
    evidence = normalize_date(
        row.values.get(config.date_column), field=config.date_column, rule=rule
    )
    warnings = list(evidence.warnings)
    if platform is None:
        warnings.append("unknown_platform")
    raw_status, raw_stage = (
        row.values.get(config.status_column),
        row.values.get(config.stage_column),
    )
    status = (
        _STATUS.get(raw_status.strip().casefold()) if type(raw_status) is str else None
    )
    stage = _STAGES.get(raw_stage.strip().upper()) if type(raw_stage) is str else None
    if status is None:
        warnings.append("unknown_status")
    if stage is None:
        warnings.append("unknown_stage")
    aliases, assigned = [], []
    for identity in config.identities:
        value = row.values.get(identity.column)
        if value is not None and value != "":
            try:
                aliases.append(
                    source_reference(
                        database=row.reference.database,
                        collection="leads",
                        key_field=identity.key_field,
                        key_type=identity.key_type,
                        value=value,
                    )
                )
            except ValueError:
                warnings.append("source_identifier_invalid")
    for staff in config.staff:
        value = row.values.get(staff.column)
        if value is None or value == "":
            continue
        try:
            ref = source_reference(
                database=row.reference.database,
                collection=staff.collection,
                key_field=staff.key_field,
                key_type=staff.key_type,
                value=value,
            )
        except ValueError:
            warnings.append("staff_identifier_invalid")
            continue
        if any(
            m.active
            and m.source_ref == ref
            and expected_site in m.sites
            and m.verified_at <= now
            for m in mappings
        ):
            assigned.append(ref)
    if not assigned:
        warnings.append("ownership_unverified")
    return NormalizedLead(
        reference=row.reference,
        platform=platform,
        status=status,
        stage=stage,
        site=site.site,
        date=evidence,
        aliases=tuple(aliases),
        assigned_to=tuple(assigned),
        warnings=tuple(sorted(set(warnings))),
    )


class NormalizationCoverage(Contract):
    scope: Literal["returned_page"] = "returned_page"
    inspected: int = Field(ge=0)
    received_verified: int = Field(ge=0)
    received_unavailable: int = Field(ge=0)
    missing_dates: int = Field(ge=0)
    unknown_platforms: int = Field(ge=0)
    ownership_unverified: int = Field(ge=0)


class NormalizedPage(Contract):
    normalization_version: Literal[1] = 1
    rows: tuple[NormalizedLead, ...]
    coverage: NormalizationCoverage
    next_position: PagePosition | None
    policy_version: int


class LeadNormalizationService:
    """Authorize before reads and recheck normalized ownership before release."""

    def __init__(
        self,
        *,
        repository: OperationalReadRepository,
        authorization: AuthorizationService,
        config: LeadAdapterConfig,
        clock,
    ):
        self._repository, self._authorization, self._config, self._clock = (
            repository,
            authorization,
            config,
            clock,
        )

    async def read_page(
        self,
        principal: Principal,
        request: ReadRequest,
        *,
        position: PagePosition | None = None,
    ) -> NormalizedPage:
        try:
            config = self._config
            if (
                not isinstance(request, ReadRequest)
                or request.binding != config.binding
                or request.fields != config.required_fields
            ):
                raise ValueError("Explicit normalization columns required")
            access = DataAccess(
                database="byd-leads-new",
                collection="leads",
                site=request.site,
                fields=request.fields,
                tool=request.tool,
                metric=request.metric,
                sensitive_breakdown=request.sensitive_breakdown,
            )
            decision = await self._authorization.authorize(
                principal, ProtectedAction(action="tool", data=(access,))
            )
            page = await self._repository.list_records(
                principal, request, position=position
            )
            if not isinstance(page, ReadPage) or len(page.rows) > request.page_size:
                raise SourceUnavailable("Source is unavailable.")
            if page.policy_version != decision.policy_version:
                raise AccessDenied("Access is not allowed.")
            mappings = tuple(
                m for m in decision.snapshot.mappings if m.user_id == principal.user_id
            )
            rows = tuple(
                _normalize_lead(
                    row,
                    config=config,
                    settings=decision.snapshot.sites,
                    mappings=mappings,
                    expected_site=request.site,
                    now=utc_datetime(self._clock()),
                )
                for row in page.rows
            )
            if len({r.reference for r in rows}) != len(rows):
                raise SourceUnavailable("Source is unavailable.")
            records = tuple(
                RecordAttributes(
                    reference=r.reference, site=r.site, assigned_to=r.assigned_to
                )
                for r in rows
            )
            await self._authorization.recheck(decision)
            final = await self._authorization.authorize(
                principal,
                ProtectedAction(
                    action="tool",
                    data=(access.model_copy(update={"records": records}),),
                ),
            )
            if final.snapshot != decision.snapshot:
                raise AccessDenied("Access is not allowed.")
            usable = sum(row.date.received_at is not None for row in rows)
            coverage = NormalizationCoverage(
                inspected=len(rows),
                received_verified=usable,
                received_unavailable=len(rows) - usable,
                missing_dates=sum(r.date.state == "missing" for r in rows),
                unknown_platforms=sum(r.platform is None for r in rows),
                ownership_unverified=sum(not r.assigned_to for r in rows),
            )
            return NormalizedPage(
                rows=rows,
                coverage=coverage,
                next_position=page.next_position,
                policy_version=decision.policy_version,
            )
        except AccessDenied:
            raise
        except Exception:
            raise SourceUnavailable("Source is unavailable.") from None


_LINKS = {
    "opportunity_customer": (
        "opportunities",
        "customers",
        "customer_id",
        "customer_id",
    ),
    "opportunity_delivery": ("opportunities", "clients", "delivery_client_id", "id"),
    "opportunity_sales_log": (
        "opportunities",
        "saleslogentries",
        "sales_log_id",
        "sales_log_id",
    ),
    "customer_delivery": ("customers", "clients", "delivery_client_id", "id"),
}


class RelationshipEvidence(Contract):
    relation: str
    state: Literal["matched", "missing", "invalid", "unresolved", "ambiguous"]
    target: SourceReference | None = None


def resolve_relationship(
    source: ReadRow,
    *,
    relation: str,
    column: str,
    authorized_targets: tuple[ReadRow, ...],
) -> RelationshipEvidence:
    """Only an authorized candidate can match; unresolved never means nonexistent.

    Source and targets must come from protected read methods. No query is issued.
    `column` must be the known canonical source field for this relation.
    """
    rule = _LINKS.get(relation)
    if rule is None or (source.reference.database, source.reference.collection) != (
        "byd-panel",
        rule[0],
    ):
        raise SourceUnavailable("Source is unavailable.")
    if column != rule[2]:
        raise SourceUnavailable("Source is unavailable.")
    value = source.values.get(column)
    if value is None or value == "":
        return RelationshipEvidence(relation=relation, state="missing")
    try:
        ref = source_reference(
            database="byd-panel",
            collection=rule[1],
            key_field=rule[3],
            key_type="string",
            value=value,
        )
    except ValueError:
        return RelationshipEvidence(relation=relation, state="invalid")
    matches = [t for t in authorized_targets if t.reference == ref]
    state = "ambiguous" if len(matches) > 1 else "matched" if matches else "unresolved"
    return RelationshipEvidence(
        relation=relation, state=state, target=ref if state == "matched" else None
    )
