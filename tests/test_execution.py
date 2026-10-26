"""Boundary tests for approved execution, count and retry limits."""

import asyncio
from dataclasses import replace
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError

from goodshowroom_ai.api_contracts import ErrorCode, TurnRequest
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.execution import (
    DEFAULT_LIMITS,
    ExecutionBudget,
    ExecutionLimitExceeded,
)
from goodshowroom_ai.operational_models import ReadRequest


def test_approved_execution_limits_are_explicit_and_finite():
    assert DEFAULT_LIMITS.request_body_bytes == 65_536
    assert DEFAULT_LIMITS.response_wire_bytes == 1_048_576
    assert DEFAULT_LIMITS.message_characters == 4_000
    assert DEFAULT_LIMITS.default_page_size == 25
    assert DEFAULT_LIMITS.max_page_size == 100
    assert DEFAULT_LIMITS.max_rows == 100
    assert DEFAULT_LIMITS.max_groups == 20
    assert DEFAULT_LIMITS.max_tool_calls == 8
    assert DEFAULT_LIMITS.context_tokens == 12_000
    assert DEFAULT_LIMITS.output_tokens == 2_000
    assert DEFAULT_LIMITS.turn_seconds == 60
    assert DEFAULT_LIMITS.turns_per_user_per_minute == 10
    assert DEFAULT_LIMITS.max_attempts == 1


@pytest.mark.parametrize(
    ("method", "allowed", "too_many", "limit"),
    [
        ("check_context_tokens", 12_000, 12_001, "context_tokens"),
        ("check_output_tokens", 2_000, 2_001, "output_tokens"),
        ("check_rows", 100, 101, "rows"),
        ("check_groups", 20, 21, "groups"),
    ],
)
def test_count_budgets_accept_limit_and_reject_next_value(
    method, allowed, too_many, limit
):
    budget = ExecutionBudget()
    check = getattr(budget, method)

    check(allowed)
    with pytest.raises(ExecutionLimitExceeded) as caught:
        check(too_many)

    assert caught.value.limit == limit
    assert str(too_many) not in str(caught.value)


def test_tool_call_and_attempt_budgets_reject_the_next_attempt():
    budget = ExecutionBudget()
    for _ in range(8):
        budget.claim_tool_call()
    with pytest.raises(ExecutionLimitExceeded) as tool_error:
        budget.claim_tool_call()
    assert tool_error.value.limit == "tool_calls"

    budget.claim_attempt()
    with pytest.raises(ExecutionLimitExceeded) as attempt_error:
        budget.claim_attempt()
    assert attempt_error.value.limit == "attempts"


def test_turn_message_and_page_inputs_are_bounded_before_business_work():
    TurnRequest(request_id=uuid4(), message="x" * 4_000, expected_version=0)
    with pytest.raises(ValidationError):
        TurnRequest(request_id=uuid4(), message="x" * 4_001, expected_version=0)

    common = {
        "binding": "leads",
        "site": "dealer_site",
        "tool": "list_leads",
        "fields": frozenset({"lead_id"}),
    }
    assert ReadRequest(**common).page_size == 25
    assert ReadRequest(**common, page_size=100).page_size == 100
    with pytest.raises(ValidationError):
        ReadRequest(**common, page_size=101)


@pytest.mark.asyncio
async def test_turn_deadline_returns_fixed_safe_timeout_error():
    limits = replace(DEFAULT_LIMITS, turn_seconds=0.01)
    budget = ExecutionBudget(limits=limits, clock=monotonic)

    with pytest.raises(ApiProblem) as caught:
        async with budget.deadline():
            await asyncio.sleep(0.1)

    assert caught.value.code is ErrorCode.DEADLINE_EXCEEDED


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_from_deadline_scope():
    budget = ExecutionBudget(limits=replace(DEFAULT_LIMITS, turn_seconds=1))
    entered = asyncio.Event()

    async def work():
        async with budget.deadline():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
