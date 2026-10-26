"""Explicit source-date provenance and verified-site reporting periods."""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from goodshowroom_ai.authorization import Contract
from goodshowroom_ai.storage.models import utc_datetime
from goodshowroom_ai.storage.settings import AppSetting

Provenance = Literal[
    "unverified", "original", "native", "imported", "scraped", "test_drive"
]
DateState = Literal[
    "valid",
    "missing",
    "invalid",
    "date_only",
    "timezone_missing",
    "ambiguous",
    "nonexistent",
]


class DateUnavailable(ValueError):
    """Safe error without raw source data."""


class DateRule(Contract):
    """Trusted code configuration, not a source-record claim or prompt."""

    provenance: Provenance = "unverified"
    review_reference: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$"
    )
    local_timezone: str | None = None

    @model_validator(mode="after")
    def reviewed(self):
        if self.provenance in {"original", "native"} and self.review_reference is None:
            raise ValueError("Received-date provenance needs reviewed evidence")
        if self.local_timezone is not None:
            try:
                ZoneInfo(self.local_timezone)
            except (ValueError, KeyError):
                raise ValueError("Source timezone is unavailable") from None
        return self


class DateEvidence(Contract):
    field: str
    state: DateState
    provenance: Provenance
    candidate_at: datetime | None = None
    calendar_date: date | None = None
    review_reference: str | None = None

    @field_validator("candidate_at")
    @classmethod
    def candidate_utc(cls, value):
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def consistent(self):
        if (self.state == "valid") != (self.candidate_at is not None):
            raise ValueError("Date evidence is inconsistent")
        if (self.state == "date_only") != (self.calendar_date is not None):
            raise ValueError("Date precision is inconsistent")
        return self

    @property
    def received_at(self) -> datetime | None:
        if (
            self.state == "valid"
            and self.provenance in {"original", "native"}
            and self.review_reference
        ):
            return self.candidate_at
        return None

    @property
    def warnings(self) -> tuple[str, ...]:
        warnings = []
        if self.state != "valid":
            warnings.append("date_" + self.state)
        if self.provenance == "unverified":
            warnings.append("date_provenance_unverified")
        elif self.provenance not in {"original", "native"}:
            warnings.append("date_not_received")
        return tuple(warnings)


def _local_instant(value: datetime, zone: ZoneInfo) -> tuple[datetime | None, str]:
    """Accept a wall time only if it maps to one real UTC instant."""
    candidates = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(candidate)
    if len(candidates) == 1:
        return candidates.pop(), "valid"
    return None, "ambiguous" if candidates else "nonexistent"


def normalize_date(value, *, field: str, rule: DateRule | None = None) -> DateEvidence:
    """No import/test-drive fallback, epoch guess, locale guess or implicit zone."""
    rule = rule or DateRule()
    state, instant, calendar = "invalid", None, None
    try:
        if value is None or value == "":
            state = "missing"
        else:
            parsed = value if type(value) is datetime else None
            if type(value) is date:
                calendar, state = value, "date_only"
            if type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                calendar, state = date.fromisoformat(value), "date_only"
            elif type(value) is str and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?",
                value,
            ):
                parsed = datetime.fromisoformat(value)
            if parsed is not None:
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    instant, state = utc_datetime(parsed), "valid"
                elif rule.local_timezone is None:
                    state = "timezone_missing"
                else:
                    instant, state = _local_instant(
                        parsed, ZoneInfo(rule.local_timezone)
                    )
    except (ValueError, OverflowError, TypeError):
        state, instant, calendar = "invalid", None, None
    return DateEvidence(
        field=field,
        state=state,
        provenance=rule.provenance,
        candidate_at=instant,
        calendar_date=calendar,
        review_reference=rule.review_reference,
    )


class ReportingPeriod(Contract):
    kind: Literal["last_seven_days", "today", "rolling_hours"]
    site: str
    timezone: str
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime

    @field_validator("start_utc", "end_utc")
    @classmethod
    def aware(cls, value):
        return utc_datetime(value)

    def contains(self, instant: datetime) -> bool:
        return self.start_utc <= utc_datetime(instant) < self.end_utc


def reporting_period(
    *,
    settings: AppSetting | None,
    site: str,
    now: datetime,
    kind: Literal["last_seven_days", "today", "rolling_hours"],
    hours: int | None = None,
) -> ReportingPeriod:
    """Previous seven completed local days; rolling periods use elapsed UTC time."""
    try:
        if not isinstance(settings, AppSetting) or settings.id != "sites":
            raise ValueError()
        config = next((s for s in settings.sites if s.site_id == site), None)
        if config is None or config.timezone is None:
            raise ValueError()
        zone = ZoneInfo(config.timezone)
        current = utc_datetime(now)
        local = current.astimezone(zone)
        if kind == "rolling_hours":
            if type(hours) is not int or hours < 1:
                raise ValueError()
            start, end = current - timedelta(hours=hours), current
        elif kind in {"today", "last_seven_days"} and hours is None:
            boundary, state = _local_instant(
                datetime.combine(local.date(), time()), zone
            )
            if state != "valid":
                raise ValueError()
            if kind == "today":
                start, end = boundary, current
            else:
                start, state = _local_instant(
                    datetime.combine(local.date() - timedelta(days=7), time()), zone
                )
                if state != "valid":
                    raise ValueError()
                end = boundary
        else:
            raise ValueError()
        return ReportingPeriod(
            kind=kind,
            site=site,
            timezone=config.timezone,
            start_local=start.astimezone(zone),
            end_local=end.astimezone(zone),
            start_utc=start,
            end_utc=end,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise DateUnavailable("Reporting period is unavailable.") from None
