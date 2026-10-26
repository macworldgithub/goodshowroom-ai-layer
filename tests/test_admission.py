"""Offline tests for per-user rate admission and per-chat turn locks."""

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest

from goodshowroom_ai.admission import AdmissionController
from goodshowroom_ai.api_contracts import ErrorCode
from goodshowroom_ai.api_http import ApiProblem


@dataclass
class FakeClock:
    value: float = 10_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


@pytest.fixture
def admission():
    clock = FakeClock()
    return AdmissionController(clock=clock), clock


@pytest.mark.asyncio
async def test_user_can_start_ten_turns_in_a_rolling_minute(admission):
    controller, _ = admission
    user_id = uuid4()

    for _ in range(10):
        async with controller.admit_turn(user_id, uuid4()):
            pass

    with pytest.raises(ApiProblem) as caught:
        async with controller.admit_turn(user_id, uuid4()):
            pytest.fail("rate-limited turn entered the admitted section")

    assert caught.value.code is ErrorCode.RATE_LIMITED


@pytest.mark.asyncio
async def test_rate_window_expires_at_sixty_seconds_and_is_per_user(admission):
    controller, clock = admission
    user_id = uuid4()

    for _ in range(10):
        async with controller.admit_turn(user_id, uuid4()):
            pass

    async with controller.admit_turn(uuid4(), uuid4()):
        pass

    clock.advance(60)
    async with controller.admit_turn(user_id, uuid4()):
        pass


@pytest.mark.asyncio
async def test_expired_user_buckets_are_swept_and_finished_chat_locks_are_removed(
    admission,
):
    controller, clock = admission
    expired_user, expired_chat = uuid4(), uuid4()
    next_user, next_chat = uuid4(), uuid4()

    async with controller.admit_turn(expired_user, expired_chat):
        pass

    assert expired_user in controller._user_turns
    assert (expired_user, expired_chat) not in controller._chat_locks

    clock.advance(61)
    async with controller.admit_turn(next_user, next_chat):
        pass

    assert expired_user not in controller._user_turns
    assert (expired_user, expired_chat) not in controller._chat_locks
    assert (next_user, next_chat) not in controller._chat_locks


@pytest.mark.asyncio
async def test_only_one_turn_can_hold_a_chat_at_a_time(admission):
    controller, _ = admission
    user_id, chat_id = uuid4(), uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_turn():
        async with controller.admit_turn(user_id, chat_id):
            entered.set()
            await release.wait()

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(entered.wait(), timeout=1)

    with pytest.raises(ApiProblem) as caught:
        async with controller.admit_turn(user_id, chat_id):
            pytest.fail("overlapping turn entered the same chat")

    assert caught.value.code is ErrorCode.REQUEST_IN_PROGRESS
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_separate_chat_can_run_while_another_chat_is_active(admission):
    controller, _ = admission
    user_id = uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_turn():
        async with controller.admit_turn(user_id, uuid4()):
            entered.set()
            await release.wait()

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(entered.wait(), timeout=1)
    async with controller.admit_turn(user_id, uuid4()):
        pass
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_cancelled_turn_releases_its_chat_lock(admission):
    controller, _ = admission
    user_id, chat_id = uuid4(), uuid4()
    entered = asyncio.Event()

    async def cancelled_turn():
        async with controller.admit_turn(user_id, chat_id):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled_turn())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with controller.admit_turn(user_id, chat_id):
        pass


@pytest.mark.asyncio
async def test_denial_does_not_echo_user_or_chat_values(admission):
    controller, _ = admission
    user_id, chat_id = uuid4(), uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_turn():
        async with controller.admit_turn(user_id, chat_id):
            entered.set()
            await release.wait()

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(ApiProblem) as caught:
        async with controller.admit_turn(user_id, chat_id):
            pass
    assert str(user_id) not in str(caught.value)
    assert str(chat_id) not in str(caught.value)
    release.set()
    await asyncio.wait_for(task, timeout=1)
