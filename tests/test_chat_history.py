"""Offline contracts for owner history, audited review, and saved-chat cleanup."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from goodshowroom_ai.authorization import AuthorizationDecision, ProtectedAction
from goodshowroom_ai.cursors import CursorRejected
from goodshowroom_ai.responses import BuiltResponse
from goodshowroom_ai.storage.chat_repository import ChatRepository
from goodshowroom_ai.storage.mongo import (
    AppDatabaseUnavailable,
    AuthorizationDenied,
    SavedChatCleanupPending,
)
from tests.test_chat_sessions import (
    FakeFindCursor,
    NOW,
    POLICY_VERSION,
    SavedChatFixture,
)
from tests.test_chat_turns import (
    TurnCollection,
    TurnFixture,
    decision_at_policy_version,
)
from tests.test_responses import complete_response


def _get_path(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


_MISSING = object()


def _matches(document, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(document, clause) for clause in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(document, clause) for clause in expected):
                return False
            continue

        actual = _get_path(document, key)
        if isinstance(expected, dict) and any(
            operator.startswith("$") for operator in expected
        ):
            for operator, value in expected.items():
                if operator == "$exists" and (actual is not _MISSING) is not value:
                    return False
                if operator == "$eq" and actual != value:
                    return False
                if operator == "$ne" and actual == value:
                    return False
                if operator == "$lt" and not (
                    actual is not _MISSING and actual < value
                ):
                    return False
                if operator == "$lte" and not (
                    actual is not _MISSING and actual <= value
                ):
                    return False
                if operator == "$gt" and not (
                    actual is not _MISSING and actual > value
                ):
                    return False
                if operator == "$gte" and not (
                    actual is not _MISSING and actual >= value
                ):
                    return False
                if operator == "$in" and actual not in value:
                    return False
        elif actual != expected:
            return False
    return True


class HistoryCollection(TurnCollection):
    """Generic bounded fake cursor and deletion failures for history contracts."""

    def __init__(self, name):
        super().__init__(name)
        self.fail_delete_many = False
        self.fail_delete_one = False
        self.before_delete_many = None
        self.before_delete_one = None
        self.delete_queries = []

    async def find_one(self, query, *, session, max_time_ms=None):
        assert session is not None
        if max_time_ms is not None:
            assert max_time_ms == 5_000
        view = self._view(session)
        found = None
        for document in view.values():
            if _matches(document, query):
                found = deepcopy(document)
                break
        if self.before_find_one is not None:
            result = self.before_find_one(query)
            if hasattr(result, "__await__"):
                await result
        return found

    def find(self, query, *, projection, collation=None, max_time_ms, session):
        assert session is not None
        assert projection.get("_id") == 1
        if collation is not None:
            assert collation.document == {"locale": "simple"}
        assert max_time_ms == 5_000
        self.queries.append(deepcopy(query))
        documents = [
            deepcopy(document)
            for document in self._view(session).values()
            if _matches(document, query)
        ]
        cursor = HistoryFindCursor(documents)
        self.cursors.append(cursor)
        return cursor

    async def delete_many(self, query, *, session):
        assert session is not None
        self.delete_queries.append(deepcopy(query))
        if self.before_delete_many is not None:
            self.before_delete_many(query, session, self._view(session))
        if self.fail_delete_many:
            self.fail_delete_many = False
            raise RuntimeError("synthetic turn cleanup detail")
        view = self._view(session)
        keys = [key for key, doc in view.items() if _matches(doc, query)]
        for key in keys:
            del view[key]
        if keys:
            self._mark_modified(session)
        return SimpleNamespace(deleted_count=len(keys))

    async def delete_one(self, query, *, session):
        assert session is not None
        self.delete_queries.append(deepcopy(query))
        if self.before_delete_one is not None:
            self.before_delete_one(query, session, self._view(session))
        if self.fail_delete_one:
            self.fail_delete_one = False
            raise RuntimeError("synthetic parent cleanup detail")
        view = self._view(session)
        key = next(
            (key for key, doc in view.items() if _matches(doc, query)),
            None,
        )
        if key is not None:
            del view[key]
            self._mark_modified(session)
        return SimpleNamespace(deleted_count=int(key is not None))


class HistoryFindCursor(FakeFindCursor):
    def sort(self, fields):
        self.sort_fields = fields
        for field, direction in reversed(fields):
            self.documents.sort(
                key=lambda document: _get_path(document, field),
                reverse=direction < 0,
            )
        return self


def history_fixture(**kwargs):
    fixture = TurnFixture(**kwargs)
    for name in ("chat_sessions", "chat_turns", "audit_events"):
        old = fixture.database[name]
        replacement = HistoryCollection(name)
        replacement.documents = old.documents
        replacement.indexes = old.indexes
        replacement.writes = old.writes
        fixture.database.collections[name] = replacement
    fixture.repository = history_repository(fixture)
    return fixture


async def _allow_turn(_decision, _turn):
    return True


def history_repository(fixture, *, turn_access_check=_allow_turn):
    return ChatRepository(
        fixture.saved.app_database,
        fixture.saved.codec,
        clock=lambda: NOW,
        authorization_recheck=fixture.authorization_recheck,
        turn_access_check=turn_access_check,
    )


async def add_turn(fixture, sequence, *, message=None, value=3):
    if fixture.parent is None:
        await fixture.create_parent()
    command = fixture.command(
        expected_version=sequence - 1,
        message=message or f"Saved question {sequence}",
    )
    attempt = await fixture.repository.begin_turn_attempt(
        fixture.decision(), fixture.parent.id, command
    )
    result = await fixture.repository.commit_completed_turn(
        fixture.decision(),
        fixture.parent.id,
        command,
        BuiltResponse(
            request_id=command.request_id,
            response=complete_response(value=value),
        ),
        attempt.run_id,
    )
    assert result.status == "committed"
    return result.turn


def _assert_content_free_review_audit(event, *, actor_id, target_id):
    assert event["actor_id"] == str(actor_id)
    assert event["target_id"] == str(target_id)
    assert event["action"] == "admin_review"
    assert event["outcome"] == "success"
    assert set(event) <= {
        "_id",
        "schema_version",
        "actor_id",
        "target_id",
        "action",
        "outcome",
        "error_category",
        "request_id",
        "created_at",
    }


def _admin_repository(owner_fixture, *, turn_access_check=_allow_turn):
    admin = SavedChatFixture()
    role = admin.role.model_copy(
        update={
            "capabilities": frozenset({"chat", "review_saved_chats"}),
        }
    )
    admin.role = role
    admin.snapshot = admin.snapshot.model_copy(update={"role": role})
    database = owner_fixture.database
    database["users"].documents[str(admin.user.id)] = admin.user.document()
    database["roles"].documents[str(role.id)] = role.document()
    database["login_sessions"].documents[str(admin.login.id)] = admin.login.document()

    calls = []

    async def recheck(decision):
        calls.append(decision)
        return decision

    repository = ChatRepository(
        owner_fixture.saved.app_database,
        owner_fixture.saved.codec,
        clock=lambda: NOW,
        authorization_recheck=recheck,
        turn_access_check=turn_access_check,
    )
    decision = AuthorizationDecision(
        principal=admin.principal,
        request=ProtectedAction(
            action="review_saved_chats",
            owner_id=owner_fixture.saved.owner_id,
            mode="saved",
        ),
        policy_version=admin.snapshot.policy_version,
        snapshot=admin.snapshot,
        constraints=(),
        requires_review_audit=True,
    )
    return admin, repository, decision, calls


@pytest.mark.asyncio
async def test_owner_history_uses_bounded_stable_encrypted_pages_and_rechecks_each_page():
    fixture = history_fixture()
    await fixture.create_parent()
    turns = [await add_turn(fixture, sequence) for sequence in range(1, 6)]
    decision = fixture.decision(action="history")

    before = len(fixture.auth_calls)
    first = await fixture.repository.list_saved_turns(
        decision, fixture.parent.id, page_size=2
    )
    after_first = len(fixture.auth_calls)
    second = await fixture.repository.list_saved_turns(
        decision,
        fixture.parent.id,
        cursor=first.next_cursor,
        page_size=2,
    )
    after_second = len(fixture.auth_calls)
    third = await fixture.repository.list_saved_turns(
        decision,
        fixture.parent.id,
        cursor=second.next_cursor,
        page_size=2,
    )

    pages = (first, second, third)
    assert tuple(view.sequence for page in pages for view in page.turns) == tuple(
        turn.sequence for turn in turns
    )
    assert [len(page.turns) for page in pages] == [2, 2, 1]
    assert first.next_cursor and second.next_cursor and third.next_cursor is None
    assert first.next_cursor.startswith("gAAAA")
    assert all(
        str(turn.id) not in first.next_cursor
        and turn.user_message not in first.next_cursor
        for turn in turns
    )
    assert after_first > before and after_second > after_first
    assert len(fixture.turns.cursors) == 3
    assert [cursor.page_limit for cursor in fixture.turns.cursors] == [3, 3, 3]
    assert all(page.notice is None for page in pages)
    assert all(
        set(view.model_dump())
        == {"id", "sequence", "user_message", "response", "committed_at"}
        for page in pages
        for view in page.turns
    )
    assert not hasattr(first, "total")

    with pytest.raises(CursorRejected):
        await fixture.repository.list_saved_turns(
            decision,
            fixture.parent.id,
            cursor=first.next_cursor,
            page_size=1,
        )


@pytest.mark.asyncio
async def test_owner_history_cursor_is_invalid_after_policy_change_or_fresh_auth_denial():
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)
    await add_turn(fixture, 2)
    decision = fixture.decision(action="history")
    page = await fixture.repository.list_saved_turns(
        decision, fixture.parent.id, page_size=1
    )
    assert page.next_cursor is not None

    fixture.database["app_settings"].documents["access_control"]["policy_version"] = (
        POLICY_VERSION + 1
    )
    newer_decision = decision_at_policy_version(decision, POLICY_VERSION + 1)
    with pytest.raises(CursorRejected):
        await fixture.repository.list_saved_turns(
            newer_decision,
            fixture.parent.id,
            cursor=page.next_cursor,
            page_size=1,
        )

    denied_fixture = history_fixture()
    await denied_fixture.create_parent()
    await add_turn(denied_fixture, 1)
    await add_turn(denied_fixture, 2)
    denied_decision = denied_fixture.decision(action="history")
    denied_page = await denied_fixture.repository.list_saved_turns(
        denied_decision, denied_fixture.parent.id, page_size=1
    )
    assert denied_page.next_cursor is not None
    before_recheck = len(denied_fixture.auth_calls)
    denied_fixture.auth_error = AuthorizationDenied("synthetic current grant denial")
    with pytest.raises(AuthorizationDenied):
        await denied_fixture.repository.list_saved_turns(
            denied_decision,
            denied_fixture.parent.id,
            cursor=denied_page.next_cursor,
            page_size=1,
        )
    assert len(denied_fixture.auth_calls) > before_recheck


@pytest.mark.asyncio
async def test_owner_history_cursor_denies_after_parent_ownership_changes_between_pages():
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)
    await add_turn(fixture, 2)
    decision = fixture.decision(action="history")
    first = await fixture.repository.list_saved_turns(
        decision, fixture.parent.id, page_size=1
    )
    assert first.next_cursor is not None

    fixture.sessions.documents[str(fixture.parent.id)]["owner_user_id"] = str(
        SavedChatFixture().owner_id
    )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.list_saved_turns(
            decision,
            fixture.parent.id,
            cursor=first.next_cursor,
            page_size=1,
        )


@pytest.mark.asyncio
async def test_unsafe_turn_is_omitted_as_a_whole_with_one_fixed_safe_notice():
    unsafe_message = "UNSAFE_SAVED_MESSAGE_SENTINEL"
    unsafe_value = 918_273
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1, message=unsafe_message, value=unsafe_value)

    checked = []

    async def current_grants(_decision, turn):
        checked.append(turn)
        return False

    fixture.repository = history_repository(
        fixture,
        turn_access_check=current_grants,
    )
    page = await fixture.repository.list_saved_turns(
        fixture.decision(action="history"), fixture.parent.id, page_size=10
    )

    encoded_page = page.model_dump_json()
    assert page.turns == ()
    assert page.notice == (
        "Some saved messages are hidden because current access does not allow them."
    )
    assert unsafe_message not in encoded_page
    assert str(unsafe_value) not in encoded_page
    assert len(checked) == 1
    assert checked[0].user_message == unsafe_message
    assert checked[0].response.facts[0].value == unsafe_value
    assert page.model_dump().keys() == {"turns", "next_cursor", "notice"}


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_result", [None, 1])
async def test_invalid_turn_grant_checker_results_fail_closed(grant_result):
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)

    async def invalid_grant_result(_decision, _turn):
        return grant_result

    fixture.repository = history_repository(
        fixture,
        turn_access_check=invalid_grant_result,
    )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.list_saved_turns(
            fixture.decision(action="history"), fixture.parent.id, page_size=10
        )


@pytest.mark.asyncio
async def test_history_fails_closed_without_current_turn_grant_checker():
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)
    fixture.repository = history_repository(fixture, turn_access_check=None)

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.list_saved_turns(
            fixture.decision(action="history"), fixture.parent.id, page_size=10
        )


@pytest.mark.asyncio
async def test_admin_review_is_separate_audited_and_does_not_impersonate_or_change_context():
    fixture = history_fixture()
    await fixture.create_parent()
    turn = await add_turn(fixture, 1)
    before_parent = deepcopy(fixture.sessions.documents[str(fixture.parent.id)])
    before_turns = deepcopy(fixture.turns.documents)
    before_write_counts = {
        name: len(collection.writes)
        for name, collection in fixture.database.collections.items()
    }
    admin, repository, decision, auth_calls = _admin_repository(fixture)

    chats = await repository.review_saved_chats(
        decision,
        owner_user_id=fixture.saved.owner_id,
        page_size=10,
    )
    turns = await repository.review_saved_turns(
        decision,
        fixture.parent.id,
        page_size=10,
    )

    assert [chat.id for chat in chats.chats] == [fixture.parent.id]
    assert set(chats.chats[0].model_dump()) == {
        "id",
        "title",
        "version",
        "last_activity_at",
    }
    assert "summary" not in chats.chats[0].model_dump()
    assert "context_state" not in chats.chats[0].model_dump()
    assert [view.sequence for view in turns.turns] == [turn.sequence]
    assert turns.turns[0].user_message == turn.user_message
    assert len(auth_calls) >= 2
    assert fixture.sessions.documents[str(fixture.parent.id)] == before_parent
    assert fixture.turns.documents == before_turns
    assert len(fixture.sessions.documents) == 1
    review_audits = [
        event
        for event in fixture.audits.documents.values()
        if event["action"] == "admin_review"
    ]
    assert len(review_audits) == 2
    for event in review_audits:
        _assert_content_free_review_audit(
            event,
            actor_id=admin.user.id,
            target_id=fixture.saved.owner_id,
        )
    for name in ("chat_sessions", "chat_turns", "users", "roles", "login_sessions"):
        assert len(fixture.database[name].writes) == before_write_counts[name]


@pytest.mark.asyncio
async def test_admin_review_rechecks_grants_and_denies_when_required_audit_fails():
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)

    async def deny_current_turn(_decision, _turn):
        return False

    admin, repository, decision, _calls = _admin_repository(
        fixture,
        turn_access_check=deny_current_turn,
    )
    page = await repository.review_saved_turns(
        decision,
        fixture.parent.id,
        page_size=10,
    )
    assert page.turns == ()
    assert page.notice == (
        "Some saved messages are hidden because current access does not allow them."
    )
    audit = next(
        event
        for event in fixture.audits.documents.values()
        if event["action"] == "admin_review"
    )
    _assert_content_free_review_audit(
        audit,
        actor_id=admin.user.id,
        target_id=fixture.saved.owner_id,
    )

    fixture.audits.fail_insert = True
    with pytest.raises(AppDatabaseUnavailable) as error:
        await repository.review_saved_turns(
            decision,
            fixture.parent.id,
            page_size=10,
        )
    assert "synthetic" not in str(error.value)
    assert (
        sum(
            event["action"] == "admin_review"
            for event in fixture.audits.documents.values()
        )
        == 1
    )

    owner_decision = fixture.decision(action="chat")
    with pytest.raises(AuthorizationDenied):
        await repository.review_saved_turns(
            owner_decision,
            fixture.parent.id,
            page_size=10,
        )
    role_without_review = decision.snapshot.role.model_copy(
        update={"capabilities": frozenset({"chat"})}
    )
    snapshot_without_review = decision.snapshot.model_copy(
        update={"role": role_without_review}
    )
    decision_without_review = decision.model_copy(
        update={"snapshot": snapshot_without_review, "requires_review_audit": False}
    )
    with pytest.raises(AuthorizationDenied):
        await repository.review_saved_turns(
            decision_without_review,
            fixture.parent.id,
            page_size=10,
        )


@pytest.mark.asyncio
async def test_admin_turn_cursor_is_bound_to_reviewer_and_target_owner():
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)
    await add_turn(fixture, 2)
    _admin_a, repository_a, decision_a, _calls = _admin_repository(fixture)
    first_page = await repository_a.review_saved_turns(
        decision_a,
        fixture.parent.id,
        page_size=1,
    )
    assert first_page.next_cursor is not None

    _admin_b, repository_b, decision_b, _other_calls = _admin_repository(fixture)
    with pytest.raises(CursorRejected):
        await repository_b.review_saved_turns(
            decision_b,
            fixture.parent.id,
            cursor=first_page.next_cursor,
            page_size=1,
        )

    different_target = decision_a.model_copy(
        update={
            "request": decision_a.request.model_copy(update={"owner_id": UUID(int=1)})
        }
    )
    with pytest.raises(CursorRejected):
        await repository_a.review_saved_turns(
            different_target,
            fixture.parent.id,
            cursor=first_page.next_cursor,
            page_size=1,
        )


@pytest.mark.asyncio
async def test_other_owner_and_temporary_decisions_cannot_read_or_delete_saved_chat():
    fixture = history_fixture()
    await fixture.create_parent()
    await add_turn(fixture, 1)
    other = SavedChatFixture()
    fixture.database["users"].documents[str(other.user.id)] = other.user.document()
    fixture.database["roles"].documents[str(other.role.id)] = other.role.document()
    fixture.database["login_sessions"].documents[str(other.login.id)] = (
        other.login.document()
    )

    async def other_recheck(decision):
        return decision

    other_repository = ChatRepository(
        fixture.saved.app_database,
        fixture.saved.codec,
        clock=lambda: NOW,
        authorization_recheck=other_recheck,
        turn_access_check=_allow_turn,
    )
    before_sessions = deepcopy(fixture.sessions.documents)
    before_turns = deepcopy(fixture.turns.documents)
    with pytest.raises(AuthorizationDenied):
        await other_repository.list_saved_turns(
            other.decision(action="history"), fixture.parent.id, page_size=10
        )
    assert (
        await other_repository.delete_saved_chat(
            other.decision(action="delete_chat"), fixture.parent.id
        )
        is None
    )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.list_saved_turns(
            fixture.decision(action="history", mode="temporary"),
            fixture.parent.id,
            page_size=10,
        )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.delete_saved_chat(
            fixture.decision(action="delete_chat", mode="temporary"),
            fixture.parent.id,
        )

    assert fixture.sessions.documents == before_sessions
    assert fixture.turns.documents == before_turns


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["turn_rows", "parent_row"])
async def test_delete_marks_parent_first_and_retry_completes_partial_cleanup(
    failure_point,
):
    fixture = history_fixture()
    await fixture.create_parent()
    turn = await add_turn(fixture, 1)
    session_id = fixture.parent.id
    session_key = str(session_id)
    turn_key = str(turn.id)

    def assert_deleting_before_cleanup(_query, session, _view):
        transaction = session.active_transaction
        assert transaction is not None
        parent_doc = transaction.collection_documents("chat_sessions")[session_key]
        assert parent_doc["status"] == "deleting"
        assert parent_doc["context_state"] is None
        assert parent_doc["summary"] is None
        assert parent_doc["last_attempt"] is None

    fixture.turns.before_delete_many = assert_deleting_before_cleanup
    if failure_point == "turn_rows":
        fixture.turns.fail_delete_many = True
    else:
        fixture.sessions.fail_delete_one = True

    with pytest.raises(AppDatabaseUnavailable):
        await fixture.repository.delete_saved_chat(
            fixture.decision(action="delete_chat"), session_id
        )

    if failure_point == "turn_rows":
        assert fixture.sessions.documents[session_key]["status"] == "deleting"
        assert turn_key in fixture.turns.documents
    else:
        assert fixture.sessions.documents[session_key]["status"] == "deleting"
        assert turn_key not in fixture.turns.documents

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.begin_turn_attempt(
            fixture.decision(), session_id, fixture.command(expected_version=1)
        )

    assert (
        await fixture.repository.delete_saved_chat(
            fixture.decision(action="delete_chat"), session_id
        )
        is None
    )
    assert session_key not in fixture.sessions.documents
    assert not fixture.turns.documents


@pytest.mark.asyncio
async def test_delete_caps_each_request_at_one_thousand_turns_and_leaves_retryable_parent():
    fixture = history_fixture()
    await fixture.create_parent()
    session_id = fixture.parent.id
    session_key = str(session_id)
    owner_key = str(fixture.saved.owner_id)
    for sequence in range(1, 1_002):
        turn_id = str(UUID(int=sequence))
        fixture.turns.documents[turn_id] = {
            "_id": turn_id,
            "session_id": session_key,
            "owner_user_id": owner_key,
            "sequence": sequence,
        }

    with pytest.raises(SavedChatCleanupPending):
        await fixture.repository.delete_saved_chat(
            fixture.decision(action="delete_chat"), session_id
        )

    assert len(fixture.turns.delete_queries) == 10
    assert (
        sum(len(query["_id"]["$in"]) for query in fixture.turns.delete_queries) == 1_000
    )
    assert all(
        len(query["_id"]["$in"]) <= 100 for query in fixture.turns.delete_queries
    )
    assert len(fixture.turns.documents) == 1
    assert fixture.sessions.documents[session_key]["status"] == "deleting"

    await fixture.repository.delete_saved_chat(
        fixture.decision(action="delete_chat"), session_id
    )
    assert not fixture.turns.documents
    assert session_key not in fixture.sessions.documents
    assert fixture.database.collections.keys() == {
        "app_settings",
        "users",
        "roles",
        "chat_sessions",
        "chat_turns",
        "audit_events",
        "login_sessions",
    }

    assert (
        await fixture.repository.delete_saved_chat(
            fixture.decision(action="delete_chat"), session_id
        )
        is None
    )
    assert session_key not in fixture.sessions.documents
    assert not fixture.turns.documents
