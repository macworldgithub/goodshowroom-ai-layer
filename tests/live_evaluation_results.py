"""Safe result records for a separately authorized future live evaluation.

This module does not invoke a model, access a network, or write files.
"""

import json
import re
from dataclasses import asdict, dataclass

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_FIELDS = frozenset(
    {
        "corpus_version",
        "prompt_version",
        "model_version",
        "metric_version",
        "case_id",
        "repetition",
        "intent_tool_arguments_correct",
        "numerical_evidence_consistent",
        "security_privacy_passed",
        "unauthorized_tool_executed",
    }
)


@dataclass(frozen=True)
class LiveResult:
    corpus_version: str
    prompt_version: str
    model_version: str
    metric_version: str
    case_id: str
    repetition: int
    intent_tool_arguments_correct: bool
    numerical_evidence_consistent: bool
    security_privacy_passed: bool
    unauthorized_tool_executed: bool


def safe_result(values: dict) -> LiveResult:
    """Accept only bounded identifiers and scores; reject unknown fields."""
    if type(values) is not dict or set(values) != _FIELDS:
        raise ValueError("Expected only the required result fields")
    for field in (
        "corpus_version",
        "prompt_version",
        "model_version",
        "metric_version",
        "case_id",
    ):
        value = values[field]
        if type(value) is not str or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"Invalid {field}")
    if type(values["repetition"]) is not int or values["repetition"] not in (1, 2, 3):
        raise ValueError("Invalid repetition")
    for field in (
        "intent_tool_arguments_correct",
        "numerical_evidence_consistent",
        "security_privacy_passed",
        "unauthorized_tool_executed",
    ):
        if type(values[field]) is not bool:
            raise ValueError(f"Invalid {field}")
    return LiveResult(**values)


def serialize_results(records: list[dict]) -> str:
    """Return safe JSON for explicit storage by a future, authorized caller."""
    validated = [safe_result(record) for record in records]
    keys = [(r.corpus_version, r.case_id, r.repetition) for r in validated]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate case repetition")
    return json.dumps([asdict(record) for record in validated], sort_keys=True)
