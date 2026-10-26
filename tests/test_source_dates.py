"""Synthetic date provenance and site-local reporting-period checks."""

from datetime import UTC, date, datetime, timedelta

import pytest
from bson import BSON
from bson.codec_options import CodecOptions
from pydantic import ValidationError

from goodshowroom_ai.source_dates import (
    DateRule,
    DateUnavailable,
    normalize_date,
    reporting_period,
)
from goodshowroom_ai.storage.settings import AppSetting, SiteConfiguration


NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
CREATED = NOW - timedelta(minutes=2)


def reviewed_rule(provenance="original", *, timezone="Australia/Melbourne"):
    return DateRule(
        provenance=provenance,
        review_reference="scope-date-review-2026-10-22",
        local_timezone=timezone,
    )


def site_settings(timezone="Australia/Melbourne"):
    return AppSetting(
        id="sites",
        sites=(SiteConfiguration(site_id="site_a", timezone=timezone),),
        created_at=CREATED,
        updated_at=NOW,
    )


def test_bson_aware_datetime_is_utc_and_usable_with_reviewed_original_rule():
    source_date = datetime(2026, 2, 3, 4, 5, 6, 789000, tzinfo=UTC)
    encoded = BSON.encode({"received": source_date})
    bson_value = BSON(encoded).decode(
        codec_options=CodecOptions(tz_aware=True, tzinfo=UTC)
    )["received"]

    evidence = normalize_date(
        bson_value,
        field="leadCreatedDate",
        rule=reviewed_rule(),
    )

    assert evidence.state == "valid"
    assert evidence.candidate_at == source_date
    assert evidence.candidate_at.tzinfo is UTC
    assert evidence.received_at == source_date
    assert evidence.provenance == "original"


@pytest.mark.parametrize("provenance", ["original", "native"])
def test_reviewed_original_or_native_source_date_can_be_used(provenance):
    source_date = datetime(2026, 3, 1, 0, 15, tzinfo=UTC)

    evidence = normalize_date(
        source_date,
        field="leadCreatedDate",
        rule=reviewed_rule(provenance),
    )

    assert evidence.state == "valid"
    assert evidence.received_at == source_date
    assert evidence.review_reference == "scope-date-review-2026-10-22"


@pytest.mark.parametrize("provenance", ["imported", "scraped", "test_drive"])
def test_imported_scraped_and_test_drive_dates_are_never_received(provenance):
    source_date = datetime(2026, 3, 1, 0, 15, tzinfo=UTC)

    evidence = normalize_date(
        source_date,
        field="leadCreatedDate",
        rule=DateRule(provenance=provenance),
    )

    assert evidence.state == "valid"
    assert evidence.candidate_at == source_date
    assert evidence.received_at is None
    assert "date_not_received" in evidence.warnings


def test_valid_unverified_date_is_not_usable():
    evidence = normalize_date(
        datetime(2026, 3, 1, tzinfo=UTC),
        field="leadCreatedDate",
    )

    assert evidence.state == "valid"
    assert evidence.provenance == "unverified"
    assert evidence.received_at is None
    assert "date_provenance_unverified" in evidence.warnings


@pytest.mark.parametrize(
    ("value", "state"),
    [
        (None, "missing"),
        ("", "missing"),
        ("not-a-date", "invalid"),
        (1_798_000_000, "invalid"),
        (True, "invalid"),
        ("2026-02-30", "invalid"),
    ],
    ids=("null", "empty", "text", "epoch-is-not-guessed", "bool", "bad-calendar"),
)
def test_missing_and_invalid_values_have_explicit_states(value, state):
    evidence = normalize_date(
        value,
        field="leadCreatedDate",
        rule=reviewed_rule(),
    )

    assert evidence.state == state
    assert evidence.received_at is None
    assert evidence.candidate_at is None


def test_calendar_date_is_not_promoted_to_an_instant():
    evidence = normalize_date(
        "2024-02-29",
        field="leadCreatedDate",
        rule=reviewed_rule(),
    )

    assert evidence.state == "date_only"
    assert evidence.calendar_date == date(2024, 2, 29)
    assert evidence.candidate_at is None
    assert evidence.received_at is None


def test_date_object_is_kept_as_calendar_date():
    evidence = normalize_date(
        date(2024, 2, 29),
        field="leadCreatedDate",
        rule=reviewed_rule(),
    )

    assert evidence.state == "date_only"
    assert evidence.calendar_date == date(2024, 2, 29)
    assert evidence.received_at is None


def test_iso_offset_date_uses_its_explicit_offset():
    evidence = normalize_date(
        "2026-01-15T12:30:00+10:00",
        field="leadCreatedDate",
        rule=reviewed_rule(timezone="America/Los_Angeles"),
    )

    assert evidence.state == "valid"
    assert evidence.candidate_at == datetime(2026, 1, 15, 2, 30, tzinfo=UTC)


def test_naive_iso_date_uses_only_the_configured_source_timezone():
    evidence = normalize_date(
        "2026-01-15T12:30:00",
        field="leadCreatedDate",
        rule=reviewed_rule(timezone="Australia/Melbourne"),
    )

    assert evidence.state == "valid"
    assert evidence.candidate_at == datetime(2026, 1, 15, 1, 30, tzinfo=UTC)


def test_naive_date_without_source_timezone_is_unavailable():
    evidence = normalize_date(
        "2026-01-15T12:30:00",
        field="leadCreatedDate",
        rule=DateRule(provenance="original", review_reference="review-1"),
    )

    assert evidence.state == "timezone_missing"
    assert evidence.received_at is None


@pytest.mark.parametrize(
    ("wall_time", "state"),
    [
        (datetime(2026, 10, 4, 2, 30), "nonexistent"),
        (datetime(2026, 4, 5, 2, 30), "ambiguous"),
    ],
    ids=("spring-gap", "autumn-fold"),
)
def test_melbourne_dst_wall_times_with_zero_or_two_instants_are_unusable(
    wall_time, state
):
    evidence = normalize_date(
        wall_time,
        field="leadCreatedDate",
        rule=reviewed_rule(),
    )

    assert evidence.state == state
    assert evidence.candidate_at is None
    assert evidence.received_at is None


def test_unknown_source_timezone_is_rejected_during_configuration():
    with pytest.raises(ValidationError):
        DateRule(
            provenance="original",
            review_reference="review-1",
            local_timezone="Mars/Olympus",
        )


def test_previous_seven_completed_local_days_can_be_167_hours_at_spring_dst():
    now = datetime(2026, 10, 10, 0, tzinfo=UTC)
    settings = site_settings()

    local_days = reporting_period(
        settings=settings, site="site_a", now=now, kind="last_seven_days"
    )
    rolling = reporting_period(
        settings=settings,
        site="site_a",
        now=now,
        kind="rolling_hours",
        hours=168,
    )

    assert local_days.start_local.date() == date(2026, 10, 3)
    assert local_days.end_local.date() == date(2026, 10, 10)
    assert (local_days.end_utc - local_days.start_utc) == timedelta(hours=167)
    assert (rolling.end_utc - rolling.start_utc) == timedelta(hours=168)


def test_previous_seven_completed_local_days_can_be_169_hours_at_fall_dst():
    now = datetime(2026, 4, 9, 12, tzinfo=UTC)

    local_days = reporting_period(
        settings=site_settings(),
        site="site_a",
        now=now,
        kind="last_seven_days",
    )
    rolling = reporting_period(
        settings=site_settings(),
        site="site_a",
        now=now,
        kind="rolling_hours",
        hours=168,
    )

    assert local_days.start_local.date() == date(2026, 4, 2)
    assert local_days.end_local.date() == date(2026, 4, 9)
    assert (local_days.end_utc - local_days.start_utc) == timedelta(hours=169)
    assert (rolling.end_utc - rolling.start_utc) == timedelta(hours=168)


def test_today_uses_local_midnight_and_an_exclusive_end():
    now = datetime(2026, 1, 15, 2, 30, tzinfo=UTC)

    period = reporting_period(
        settings=site_settings(), site="site_a", now=now, kind="today"
    )

    assert period.start_local.date() == date(2026, 1, 15)
    assert period.start_local.hour == 0
    assert period.end_utc == now
    assert period.contains(period.start_utc)
    assert period.contains(period.end_utc - timedelta(microseconds=1))
    assert not period.contains(period.end_utc)


def test_seven_completed_days_include_leap_day_with_inclusive_start_and_exclusive_end():
    now = datetime(2024, 3, 1, 1, tzinfo=UTC)

    period = reporting_period(
        settings=site_settings("UTC"),
        site="site_a",
        now=now,
        kind="last_seven_days",
    )

    assert period.start_local.date() == date(2024, 2, 23)
    assert period.end_local.date() == date(2024, 3, 1)
    assert period.contains(period.start_utc)
    assert period.contains(datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC))
    assert not period.contains(period.end_utc)


@pytest.mark.parametrize(
    "settings",
    [
        None,
        AppSetting(
            id="sites",
            sites=(SiteConfiguration(site_id="site_a", timezone=None),),
            created_at=CREATED,
            updated_at=NOW,
        ),
        AppSetting(
            id="sites",
            sites=(SiteConfiguration(site_id="other_site", timezone="UTC"),),
            created_at=CREATED,
            updated_at=NOW,
        ),
    ],
    ids=("missing-settings", "missing-site-timezone", "missing-site"),
)
def test_reporting_period_fails_closed_without_configured_site_timezone(settings):
    with pytest.raises(DateUnavailable) as error:
        reporting_period(
            settings=settings,
            site="sensitive-site-name",
            now=NOW,
            kind="last_seven_days",
        )

    assert str(error.value) == "Reporting period is unavailable."
    assert "sensitive-site-name" not in str(error.value)
