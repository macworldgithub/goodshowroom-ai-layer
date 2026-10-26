"""TICKET-009 saved context reads stay within a current app snapshot."""

from copy import deepcopy
from datetime import timedelta

import pytest

from goodshowroom_ai.storage.chat_repository import ChatRepository
from goodshowroom_ai.storage.mongo import AppWriteConflict, AuthorizationDenied
from tests.test_chat_sessions import FakeFindCursor
from tests.test_chat_turns import (
    NOW,
    TurnFixture,
    commit_attempt,
    start_attempt,
)


class HistoryTurnCollection:
    """Bounded in-memory reader for the existing snapshot transaction API."""

    def __init__(self, documents):
        self.documents = deepcopy(documents)
        self.queries = []
        self.cursors = []

    def find(self, query, *, projection, max_time_ms, session):
        assert session is not None
        assert max_time_ms == 5_000
        assert "user_message" in projection
        assert "response" in projection
        self.queries.append(deepcopy(query))
        sequence = query["sequence"]
        documents = [
            deepcopy(document)
            for document in self.documents.values()
            if document["session_id"] == query["session_id"]
            and document["owner_user_id"] == query["owner_user_id"]
            and sequence["$gt"] < document["sequence"] <= sequence["$lte"]
        ]
        documents.sort(key=lambda document: document["sequence"])
        cursor = FakeFindCursor(documents)
        self.cursors.append(cursor)
        return cursor


async def _repository_with_turns(fixture, *, access_check):
    turn_collection = HistoryTurnCollection(fixture.turns.documents)
    fixture.database.collections["chat_turns"] = turn_collection
    repository = ChatRepository(
        fixture.saved.app_database,
        fixture.saved.codec,
        clock=lambda: NOW,
        authorization_recheck=fixture.authorization_recheck,
        turn_access_check=access_check,
    )
    fixture.repository = repository
    return repository, turn_collection


async def _commit_two_turns(fixture):
    await fixture.create_parent()
    first = fixture.command(message="First saved question")
    first_attempt = await start_attempt(fixture, first)
    assert (await commit_attempt(fixture, first, first_attempt)).status == "committed"
    second = fixture.command(expected_version=1, message="Second saved question")
    second_attempt = await start_attempt(fixture, second)
    assert (await commit_attempt(fixture, second, second_attempt)).status == "committed"


@pytest.mark.asyncio
async def test_agent_history_reads_recent_complete_turns_and_reauthorizes_each_one():
    fixture = TurnFixture()
    await _commit_two_turns(fixture)
    accessed = []

    async def allow_second_only(decision, turn):
        accessed.append((decision, turn.sequence))
        return turn.sequence == 2

    repository, turns = await _repository_with_turns(
        fixture, access_check=allow_second_only
    )
    decision = fixture.decision(action="history")
    auth_calls_before = len(fixture.auth_calls)

    history = await repository.load_agent_history(
        decision, fixture.parent.id, expected_version=2
    )

    assert history.owner_id == fixture.saved.owner_id
    assert history.session_id == fixture.parent.id
    assert history.mode == "saved"
    assert history.version == 2
    assert history.policy_version == decision.policy_version
    assert [turn.sequence for turn in history.turns] == [2]
    assert history.turns[0].user_message == "Second saved question"
    assert history.state is not None
    assert history.state.through_sequence == 2
    assert [sequence for _decision, sequence in accessed] == [1, 2]
    assert len(fixture.auth_calls) == auth_calls_before + 1
    assert turns.queries[0]["sequence"] == {"$gt": 0, "$lte": 2}
    assert turns.queries[0]["session_id"] == str(fixture.parent.id)
    assert turns.queries[0]["owner_user_id"] == str(fixture.saved.owner_id)
    assert turns.cursors[0].page_limit == 21
    assert turns.cursors[0].closed == 1


@pytest.mark.asyncio
async def test_agent_history_rejects_stale_version_before_reading_any_turn():
    fixture = TurnFixture()
    await _commit_two_turns(fixture)
    accessed = []

    async def allow_all(_decision, turn):
        accessed.append(turn.sequence)
        return True

    repository, turns = await _repository_with_turns(fixture, access_check=allow_all)

    with pytest.raises(AppWriteConflict):
        await repository.load_agent_history(
            fixture.decision(action="history"), fixture.parent.id, expected_version=1
        )

    assert turns.queries == []
    assert accessed == []


@pytest.mark.asyncio
async def test_agent_history_fails_closed_for_expired_parent_and_current_policy_change():
    fixture = TurnFixture()
    await _commit_two_turns(fixture)
    accessed = []

    async def allow_all(_decision, turn):
        accessed.append(turn.sequence)
        return True

    repository, turns = await _repository_with_turns(fixture, access_check=allow_all)
    parent_document = fixture.sessions.documents[str(fixture.parent.id)]
    parent_document["created_at"] = NOW - timedelta(hours=2)
    parent_document["last_activity_at"] = NOW - timedelta(hours=1)
    parent_document["expires_at"] = NOW - timedelta(seconds=1)

    with pytest.raises(AuthorizationDenied):
        await repository.load_agent_history(
            fixture.decision(action="history"), fixture.parent.id, expected_version=2
        )
    assert accessed == []
    assert turns.queries == []

    parent_document["expires_at"] = None
    fixture.database.collections["app_settings"].documents["access_control"][
        "policy_version"
    ] += 1
    with pytest.raises(AppWriteConflict):
        await repository.load_agent_history(
            fixture.decision(action="history"), fixture.parent.id, expected_version=2
        )
    assert accessed == []
    assert turns.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("checker_mode", ["missing", "raises"])
async def test_agent_history_denies_when_per_turn_access_checker_is_missing_or_fails(
    checker_mode,
):
    fixture = TurnFixture()
    await _commit_two_turns(fixture)
    checker_calls = []

    async def broken_checker(_decision, _turn):
        checker_calls.append(True)
        raise RuntimeError("synthetic private checker failure")

    checker = None if checker_mode == "missing" else broken_checker
    repository, turns = await _repository_with_turns(fixture, access_check=checker)

    with pytest.raises(AuthorizationDenied) as caught:
        await repository.load_agent_history(
            fixture.decision(action="history"), fixture.parent.id, expected_version=2
        )

    assert "synthetic private checker failure" not in str(caught.value)
    if checker_mode == "missing":
        assert turns.queries == []
        assert checker_calls == []
    else:
        assert len(turns.queries) == 1
        assert checker_calls == [True]
