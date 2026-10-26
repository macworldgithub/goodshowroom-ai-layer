"""Typed internal contracts. No Mongo query/pipeline is accepted from callers."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from goodshowroom_ai.authorization import Contract
from goodshowroom_ai.execution import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from goodshowroom_ai.storage.identities import SourceReference
from goodshowroom_ai.storage.models import utc_datetime

Scalar = str | int | float | bool | datetime | None
Q01_TOOL = "count_leads"
Kind = Literal[
    "string", "objectid", "integer", "number", "boolean", "date", "source_date"
]


class FieldBinding(Contract):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$")
    path: str = Field(
        max_length=200, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$"
    )
    kind: Kind

    @field_validator("path")
    @classmethod
    def no_credentials(cls, value):
        if value.split(".")[0] == "_joined" or re.search(
            r"password|token|secret|credential|private.?key", value, re.I
        ):
            raise ValueError("Sensitive source fields are unavailable")
        return value


class StaffBinding(Contract):
    field: str
    database: Literal["byd-leads-new", "byd-panel"]
    collection: Literal["users", "staff_site_memberships"]
    key_field: Literal["_id", "id", "user_id", "staff_id"]
    key_type: Literal["string", "objectid"]


class SiteBinding(Contract):
    site: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    values: tuple[str, ...] = Field(min_length=1, max_length=100)


class CollectionBinding(Contract):
    """Trusted, reviewed adapter configuration; no production defaults."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    database: Literal["byd-leads-new", "byd-panel"]
    collection: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    fields: tuple[FieldBinding, ...] = Field(min_length=1, max_length=100)
    identity: str
    site_field: str
    sites: tuple[SiteBinding, ...] = Field(min_length=1, max_length=100)
    staff: tuple[StaffBinding, ...] = Field(default=(), max_length=10)
    mongo_id_type: Literal["string", "objectid"] = "objectid"

    @model_validator(mode="after")
    def consistent(self):
        fields = {f.name: f for f in self.fields}
        if len(fields) != len(self.fields) or len({f.path for f in self.fields}) != len(
            self.fields
        ):
            raise ValueError("Ambiguous field bindings")
        if self.identity not in fields or fields[self.identity].kind not in {
            "string",
            "objectid",
        }:
            raise ValueError("Stable identity binding required")
        if "." in fields[self.identity].path:
            raise ValueError("Direct identity field required")
        if self.site_field not in fields or fields[self.site_field].kind != "string":
            raise ValueError("Site binding required")
        for staff in self.staff:
            if (
                staff.database != self.database
                or staff.field not in fields
                or fields[staff.field].kind != staff.key_type
            ):
                raise ValueError("Qualified staff binding required")
        values = [v for site in self.sites for v in site.values]
        if (
            len(set(values)) != len(values)
            or len({s.site for s in self.sites}) != len(self.sites)
            or any(
                not v.strip()
                or len(v) > 200
                or v.strip().casefold() in {"all", "all sites", "all locations", "*"}
                for v in values
            )
        ):
            raise ValueError("Unambiguous explicit site values required")
        if any(f.path == "_id" and f.kind != self.mongo_id_type for f in self.fields):
            raise ValueError("Mongo identifier type mismatch")
        return self


class JoinBinding(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    left: str
    right: str
    left_field: str
    right_field: str


class EqualFilter(Contract):
    field: str
    value: Scalar


class DateFilter(Contract):
    field: str
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def dates(cls, value):
        return utc_datetime(value)

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("Empty date interval")
        return self


class ReadRequest(Contract):
    binding: str
    site: str
    tool: str
    fields: frozenset[str] = Field(min_length=1, max_length=100)
    filters: tuple[EqualFilter, ...] = Field(default=(), max_length=20)
    dates: DateFilter | None = None
    metric: str | None = None
    sensitive_breakdown: bool = False
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class PagePosition(Contract):
    """Internal continuation only. TICKET-057 owns encrypted HTTP cursors."""

    user_id: UUID
    policy_version: int = Field(ge=0)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    after: SourceReference


class ReadRow(Contract):
    reference: SourceReference
    values: dict[str, Scalar]


class ReadPage(Contract):
    rows: tuple[ReadRow, ...]
    next_position: PagePosition | None = None
    policy_version: int


class JoinedRow(Contract):
    left: ReadRow
    right: tuple[ReadRow, ...]


class JoinedPage(Contract):
    rows: tuple[JoinedRow, ...]
    next_position: PagePosition | None = None
    policy_version: int


class CountResult(Contract):
    count: int = Field(ge=0)
    policy_version: int


class DateCohortPlan(Contract):
    """Internal classification plan, never model/browser arguments or Mongo JSON."""

    platform_field: str
    date_field: str
    eligible_platforms: frozenset[str] = Field(min_length=1, max_length=20)
    excluded_platforms: frozenset[str] = Field(default_factory=frozenset, max_length=20)
    reliable_platforms: frozenset[str] = Field(default_factory=frozenset, max_length=20)
    period: DateFilter

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.eligible_platforms & self.excluded_platforms
            or not self.reliable_platforms <= self.eligible_platforms
        ):
            raise ValueError("Invalid source classification")
        if self.period.field != self.date_field:
            raise ValueError("Date field mismatch")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", name)
            for name in self.eligible_platforms
            | self.excluded_platforms
            | self.reliable_platforms
        ):
            raise ValueError("Invalid source code")
        return self


class DateCohortCounts(Contract):
    candidates: int = Field(ge=0)
    eligible: int = Field(ge=0)
    excluded: int = Field(ge=0)
    reliable: int = Field(ge=0)
    in_period: int = Field(ge=0)
    policy_version: int

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.eligible + self.excluded > self.candidates
            or not self.in_period <= self.reliable <= self.eligible
        ):
            raise ValueError("Inconsistent source counts")
        return self
