"""Validate safe recording without running a live model."""

import json

import pytest

from tests.live_evaluation_results import safe_result, serialize_results


def record():
    return {
        "corpus_version": "q01-synthetic-v1",
        "prompt_version": "prompt-v1",
        "model_version": "model-v1",
        "metric_version": "1",
        "case_id": "q01-1",
        "repetition": 1,
        "intent_tool_arguments_correct": True,
        "numerical_evidence_consistent": True,
        "security_privacy_passed": True,
        "unauthorized_tool_executed": False,
    }


def test_serializes_only_safe_fields_and_repetitions():
    records = [dict(record(), repetition=i) for i in (1, 2, 3)]
    assert json.loads(serialize_results(records)) == records


@pytest.mark.parametrize(
    "field", ["corpus_version", "prompt_version", "model_version", "metric_version"]
)
def test_missing_or_empty_version_fails(field):
    values = record()
    del values[field]
    with pytest.raises(ValueError):
        safe_result(values)
    values = record()
    values[field] = ""
    with pytest.raises(ValueError):
        safe_result(values)


@pytest.mark.parametrize(
    "field", ["question", "output", "credential", "token", "prompt"]
)
def test_raw_data_and_credentials_are_rejected(field):
    with pytest.raises(ValueError):
        safe_result(dict(record(), **{field: "sensitive data"}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_id", "a question?"),
        ("repetition", True),
        ("repetition", 4),
        ("security_privacy_passed", 1),
    ],
)
def test_invalid_values_fail(field, value):
    with pytest.raises(ValueError):
        safe_result(dict(record(), **{field: value}))


def test_duplicate_repetition_fails():
    with pytest.raises(ValueError):
        serialize_results([record(), record()])
