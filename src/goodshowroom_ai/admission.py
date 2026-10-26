"""Single-process turn admission, rate limits, and per-chat exclusion."""

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID

from goodshowroom_ai.api_contracts import ErrorCode
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.execution import DEFAULT_LIMITS, ExecutionLimits


class AdmissionController:
    """Reject busy chats and excess user turns before paid or source work."""

    def __init__(
        self,
        *,
        limits: ExecutionLimits = DEFAULT_LIMITS,
        clock: Callable[[], float] = monotonic,
    ):
        if not isinstance(limits, ExecutionLimits) or not callable(clock):
            raise ValueError("Admission configuration is invalid")
        self._limits = limits
        self._clock = clock
        self._window = 60.0
        self._guard = asyncio.Lock()
        self._chat_locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
        self._user_turns: dict[UUID, deque[float]] = {}
        self._next_sweep = 0.0

    @asynccontextmanager
    async def admit_turn(self, user_id: UUID, chat_id: UUID):
        if type(user_id) is not UUID or type(chat_id) is not UUID:
            raise ApiProblem(ErrorCode.INVALID_REQUEST)

        key = (user_id, chat_id)
        acquired = False
        async with self._guard:
            now = self._clock()
            if now >= self._next_sweep:
                cutoff = now - self._window
                for known_user, known_turns in tuple(self._user_turns.items()):
                    while known_turns and known_turns[0] <= cutoff:
                        known_turns.popleft()
                    if not known_turns:
                        self._user_turns.pop(known_user, None)
                self._next_sweep = now + min(self._window, 30.0)

            lock = self._chat_locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                raise ApiProblem(ErrorCode.REQUEST_IN_PROGRESS)

            timestamps = self._user_turns.setdefault(user_id, deque())
            cutoff = now - self._window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._limits.turns_per_user_per_minute:
                raise ApiProblem(ErrorCode.RATE_LIMITED)

            await lock.acquire()
            acquired = True
            timestamps.append(now)

        try:
            yield
        finally:
            if acquired:
                lock.release()
                async with self._guard:
                    if not lock.locked() and self._chat_locks.get(key) is lock:
                        self._chat_locks.pop(key, None)
                    timestamps = self._user_turns.get(user_id)
                    if timestamps is not None and not timestamps:
                        self._user_turns.pop(user_id, None)
