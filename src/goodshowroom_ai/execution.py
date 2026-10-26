"""Approved, deterministic limits for one backend turn."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    request_body_bytes: int = 65_536
    response_wire_bytes: int = 1_048_576
    message_characters: int = 4_000
    default_page_size: int = 25
    max_page_size: int = 100
    max_rows: int = 100
    max_groups: int = 20
    max_tool_calls: int = 8
    context_tokens: int = 12_000
    output_tokens: int = 2_000
    turn_seconds: float = 60.0
    turns_per_user_per_minute: int = 10
    max_attempts: int = 1
    json_depth: int = 20
    json_nodes: int = 20_000

    def __post_init__(self) -> None:
        integer_fields = (
            "request_body_bytes",
            "response_wire_bytes",
            "message_characters",
            "default_page_size",
            "max_page_size",
            "max_rows",
            "max_groups",
            "max_tool_calls",
            "context_tokens",
            "output_tokens",
            "turns_per_user_per_minute",
            "max_attempts",
            "json_depth",
            "json_nodes",
        )
        if any(type(getattr(self, name)) is not int for name in integer_fields):
            raise ValueError("Execution limits must be integers")
        if any(getattr(self, name) < 1 for name in integer_fields):
            raise ValueError("Execution limits must be positive")
        if self.default_page_size > self.max_page_size:
            raise ValueError("Default page size exceeds the maximum")
        if type(self.turn_seconds) not in {int, float} or self.turn_seconds <= 0:
            raise ValueError("Turn deadline must be positive")
        if self.max_attempts != 1:
            raise ValueError("Automatic retries are disabled until classified")


DEFAULT_LIMITS = ExecutionLimits()
MAX_REQUEST_BYTES = DEFAULT_LIMITS.request_body_bytes
MAX_WIRE_BYTES = DEFAULT_LIMITS.response_wire_bytes
MAX_MESSAGE_CHARACTERS = DEFAULT_LIMITS.message_characters
DEFAULT_PAGE_SIZE = DEFAULT_LIMITS.default_page_size
MAX_PAGE_SIZE = DEFAULT_LIMITS.max_page_size
MAX_JSON_DEPTH = DEFAULT_LIMITS.json_depth
MAX_JSON_NODES = DEFAULT_LIMITS.json_nodes


class ExecutionLimitExceeded(Exception):
    """A fixed internal limit category without raw input or measured values."""

    _ALLOWED = frozenset(
        {
            "tool_calls",
            "attempts",
            "context_tokens",
            "output_tokens",
            "rows",
            "groups",
        }
    )

    def __init__(self, limit: str):
        if limit not in self._ALLOWED:
            raise ValueError("Invalid limit category")
        self.limit = limit
        super().__init__("Execution limit exceeded.")


class ExecutionBudget:
    """Track one turn's time and bounded calls without truncating its evidence."""

    def __init__(
        self,
        limits: ExecutionLimits = DEFAULT_LIMITS,
        *,
        clock: Callable[[], float] = monotonic,
    ):
        if not isinstance(limits, ExecutionLimits) or not callable(clock):
            raise ValueError("Execution budget configuration is invalid")
        self.limits = limits
        self._clock = clock
        self._deadline = clock() + limits.turn_seconds
        self._tool_calls = 0
        self._attempts = 0
        self._entered = False

    @staticmethod
    def _deadline_problem():
        # Import locally because api_http uses this module's shared limits.
        from goodshowroom_ai.api_contracts import ErrorCode
        from goodshowroom_ai.api_http import ApiProblem

        return ApiProblem(ErrorCode.DEADLINE_EXCEEDED)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def claim_tool_call(self) -> None:
        if self._tool_calls >= self.limits.max_tool_calls:
            raise ExecutionLimitExceeded("tool_calls")
        self._tool_calls += 1

    def claim_attempt(self) -> None:
        if self._attempts >= self.limits.max_attempts:
            raise ExecutionLimitExceeded("attempts")
        self._attempts += 1

    def check_context_tokens(self, count: int) -> None:
        self._check_count(count, self.limits.context_tokens, "context_tokens")

    def check_output_tokens(self, count: int) -> None:
        self._check_count(count, self.limits.output_tokens, "output_tokens")

    def check_rows(self, count: int) -> None:
        self._check_count(count, self.limits.max_rows, "rows")

    def check_groups(self, count: int) -> None:
        self._check_count(count, self.limits.max_groups, "groups")

    @staticmethod
    def _check_count(count: int, maximum: int, limit: str) -> None:
        if type(count) is not int or count < 0 or count > maximum:
            raise ExecutionLimitExceeded(limit)

    async def __aenter__(self):
        if self._entered:
            raise ValueError("Execution budget can only be entered once")
        self._entered = True
        if self.remaining_seconds <= 0:
            raise self._deadline_problem() from None
        self._timeout = asyncio.timeout(self.remaining_seconds)
        try:
            await self._timeout.__aenter__()
        except TimeoutError:
            raise self._deadline_problem() from None
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            await self._timeout.__aexit__(exc_type, exc, traceback)
        except TimeoutError:
            raise self._deadline_problem() from None
        if self._timeout.expired():
            raise self._deadline_problem() from None
        return False

    def deadline(self):
        """Return an async context manager that cancels work at the turn limit."""
        return self
