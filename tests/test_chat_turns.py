"""Offline saved-turn contracts with a rollback-aware fake app database.

These tests exercise repository behavior. They do not prove MongoDB transaction
semantics or actual unique-index behavior.
"""

import asyncio
from copy import deepcopy
from datetime import timedelta
import inspect
import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pymongo.errors import OperationFailure

import goodshowroom_ai.storage.chat_repository as chat_repository_module
from goodshowroom_ai.responses import BuiltResponse, Coverage, StructuredResponse
from goodshowroom_ai.storage.chat_repository import ChatRepository
from goodshowroom_ai.storage.chat_turns import (
    ChatTurnRecord,
    TurnCommand,
    TurnRecoveryResult,
)
from goodshowroom_ai.storage.models import validate_bson_document
from goodshowroom_ai.storage.mongo import (
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
)
from tests.test_chat_sessions import (
    ChatCollection,
    NOW,
    POLICY_VERSION,
    SavedChatFixture,
)
from tests.test_responses import complete_response


class RollbackTransaction:
    """Keep a snapshot until commit and detect competing writes."""

    def __init__(self, events, database, session):
        self.events = events
        self.database = database
        self.session = session
        self.snapshot = None

    async def __aenter__(self):
        self.events.append("transaction-start")
        self.snapshot = {
            name: deepcopy(collection.documents)
            for name, collection in self.database.collections.items()
        }
        self.working = deepcopy(self.snapshot)
        self.modified = set()
        self.session.active_transaction = self
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self.events.append("transaction-abort")
            self.session.active_transaction = None
            return False

        try:
            for name in self.modified:
                if self.database.collections[name].documents != self.snapshot[name]:
                    self.events.append("transaction-abort")
                    raise AppWriteConflict("Synthetic transaction write conflict.")
            if (
                self.session.commit_result_unknown
                and not self.session.commit_unknown_is_persisted
            ):
                self.session.commit_result_unknown = False
                self.events.append("transaction-commit-unknown")
                raise OperationFailure(
                    "synthetic uncertain commit",
                    code=91,
                    details={"errorLabels": ["UnknownTransactionCommitResult"]},
                )
            for name in self.modified:
                self.database.collections[name].documents = deepcopy(self.working[name])
            self.events.append("transaction-commit")
            if self.session.commit_result_unknown:
                self.session.commit_result_unknown = False
                raise OperationFailure(
                    "synthetic uncertain commit",
                    code=91,
                    details={"errorLabels": ["UnknownTransactionCommitResult"]},
                )
            return False
        finally:
            self.session.active_transaction = None

    def collection_documents(self, name):
        return self.working.setdefault(name, {})

    def mark_modified(self, name):
        self.modified.add(name)


class RollbackSession:
    def __init__(
        self,
        events,
        database,
        *,
        commit_result_unknown=False,
        commit_unknown_is_persisted=True,
    ):
        self.events = events
        self.database = database
        self.active_transaction = None
        self.commit_result_unknown = commit_result_unknown
        self.commit_unknown_is_persisted = commit_unknown_is_persisted

    def bind(self):
        return self

    async def __aenter__(self):
        self.events.append("session-start")
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.events.append("session-close")
        return False

    async def start_transaction(self, **_options):
        return RollbackTransaction(self.events, self.database, self)


class TwoPartyBarrier:
    """Hold two repository calls at the same read before either continues."""

    def __init__(self):
        self.arrivals = 0
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_for_two(self):
        if self.arrivals < 2:
            self.arrivals += 1
            if self.arrivals == 2:
                self.ready.set()
            await self.release.wait()


class TurnCollection(ChatCollection):
    """Snapshot-aware fake collection with dotted fields and failure controls."""

    def __init__(self, name):
        super().__init__(name)
        self.fail_replace = False
        self.fail_update = False
        self.fail_index = False
        self.before_find_one = None
        self.before_update_one = None

    async def create_index(self, keys, **options):
        if self.fail_index:
            raise RuntimeError("synthetic index failure")
        return await super().create_index(keys, **options)

    def _view(self, session):
        transaction = getattr(session, "active_transaction", None)
        if transaction is None:
            return self.documents
        return transaction.collection_documents(self.name)

    def _mark_modified(self, session):
        transaction = getattr(session, "active_transaction", None)
        if transaction is not None:
            transaction.mark_modified(self.name)

    @staticmethod
    def _get_path(document, path):
        value = document
        for part in path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    @staticmethod
    def _set_path(document, path, value):
        parts = path.split(".")
        target = document
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = deepcopy(value)

    async def find_one(self, query, *, session):
        assert session is not None
        view = self._view(session)
        found = None
        for document in view.values():
            if all(
                self._get_path(document, key) == value for key, value in query.items()
            ):
                found = deepcopy(document)
                break
        if self.before_find_one is not None:
            result = self.before_find_one(query)
            if inspect.isawaitable(result):
                await result
        return found

    async def insert_one(self, document, *, session):
        assert session is not None
        self.writes.append(("insert", deepcopy(document), session))
        if self.fail_insert:
            raise RuntimeError("synthetic insert failure")
        view = self._view(session)
        if document["_id"] in view:
            raise RuntimeError("synthetic duplicate ID")
        view[document["_id"]] = deepcopy(document)
        self._mark_modified(session)
        return SimpleNamespace(inserted_id=document["_id"])

    async def replace_one(self, query, document, *, session):
        if self.fail_replace:
            raise RuntimeError("synthetic parent update failure")
        self.writes.append(("replace", deepcopy(query), deepcopy(document), session))
        view = self._view(session)
        current = view.get(query.get("_id"))
        matched = current is not None and all(
            self._get_path(current, key) == value for key, value in query.items()
        )
        if matched:
            view[document["_id"]] = deepcopy(document)
            self._mark_modified(session)
        return SimpleNamespace(matched_count=int(matched))

    async def update_one(self, query, update, *, session):
        if self.fail_update:
            raise RuntimeError("synthetic parent update failure")
        assert session is not None
        if self.before_update_one is not None:
            self.before_update_one(query, update)
        self.writes.append(("update", deepcopy(query), deepcopy(update), session))
        view = self._view(session)
        current = view.get(query.get("_id"))
        matched = current is not None and all(
            self._get_path(current, key) == value for key, value in query.items()
        )
        if not matched:
            return SimpleNamespace(matched_count=0)
        for path, value in update.get("$set", {}).items():
            self._set_path(current, path, value)
        for path, amount in update.get("$inc", {}).items():
            current[path] = current.get(path, 0) + amount
        self._mark_modified(session)
        return SimpleNamespace(matched_count=1)


class TurnFixture:
    def __init__(self, *, owner_id=None, expires_at=None, auth_error=None):
        self.saved = SavedChatFixture(owner_id=owner_id)
        database = self.saved.client.database
        self.next_session_settings = {}
        self.transaction_sessions = []

        def start_session():
            session = RollbackSession(
                self.saved.client.events,
                database,
                **self.next_session_settings,
            )
            self.next_session_settings = {}
            self.transaction_sessions.append(session)
            return session

        self.saved.client.start_session = start_session
        for name in ("app_settings", "users", "roles", "chat_sessions"):
            old_collection = database.collections[name]
            collection = TurnCollection(name)
            collection.documents = old_collection.documents
            collection.indexes = old_collection.indexes
            collection.writes = old_collection.writes
            database.collections[name] = collection
        database.collections["chat_turns"] = TurnCollection("chat_turns")
        database.collections["audit_events"] = TurnCollection("audit_events")
        database.collections["login_sessions"] = TurnCollection("login_sessions")
        database.collections["login_sessions"].documents = {
            str(self.saved.login.id): self.saved.login.document()
        }
        self.auth_error = auth_error
        self.auth_calls = []

        async def authorization_recheck(*args, **kwargs):
            self.auth_calls.append((args, kwargs))
            if self.auth_error is not None:
                raise self.auth_error
            return args[0]

        self.authorization_recheck = authorization_recheck
        self.repository = self.new_repository()
        self.parent = None
        self.expires_at = expires_at

    def new_repository(self):
        return ChatRepository(
            self.saved.app_database,
            self.saved.codec,
            clock=lambda: NOW,
            authorization_recheck=self.authorization_recheck,
        )

    async def create_parent(self):
        self.parent = await self.repository.create_saved_chat(self.decision(), uuid4())
        if self.expires_at is not None:
            document = self.sessions.documents[str(self.parent.id)]
            document["expires_at"] = self.expires_at
            self.parent = self.parent.model_copy(update={"expires_at": self.expires_at})
        return self.parent

    def decision(self, *, action="chat", mode="saved"):
        return self.saved.decision(action=action, mode=mode)

    @property
    def database(self):
        return self.saved.client.database

    @property
    def sessions(self):
        return self.database["chat_sessions"]

    @property
    def turns(self):
        return self.database["chat_turns"]

    @property
    def audits(self):
        return self.database["audit_events"]

    @property
    def events(self):
        return self.saved.client.events

    def command(
        self, *, request_id=None, expected_version=0, message="Show total leads"
    ):
        return TurnCommand(
            request_id=request_id or uuid4(),
            expected_version=expected_version,
            message=message,
        )

    def built_response(self, command):
        return BuiltResponse(
            request_id=command.request_id,
            response=complete_response(),
        )

    def make_next_commit_unknown(self, *, persisted=True):
        self.next_session_settings = {
            "commit_result_unknown": True,
            "commit_unknown_is_persisted": persisted,
        }


async def start_attempt(fixture, command):
    if fixture.parent is None:
        await fixture.create_parent()
    return await fixture.repository.begin_turn_attempt(
        fixture.decision(), fixture.parent.id, command
    )


async def commit_attempt(fixture, command, attempt):
    return await fixture.repository.commit_completed_turn(
        fixture.decision(),
        fixture.parent.id,
        command,
        fixture.built_response(command),
        attempt.run_id,
    )


def only_record(collection):
    assert len(collection.documents) == 1
    return next(iter(collection.documents.values()))


def notice_response(command, policy_version):
    return BuiltResponse(
        request_id=command.request_id,
        response=StructuredResponse(
            status="unsupported",
            answer="This question is not supported yet.",
            warnings=("question_not_supported",),
            coverage=Coverage(status="unknown"),
            policy_version=policy_version,
        ),
    )


def decision_at_policy_version(decision, policy_version):
    snapshot = decision.snapshot.model_copy(update={"policy_version": policy_version})
    return decision.model_copy(
        update={"policy_version": policy_version, "snapshot": snapshot}
    )


@pytest.mark.asyncio
async def test_turn_command_is_strict_and_attempt_stores_no_message_or_sdk_content():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command(
        message="A private question that must not enter attempt state"
    )

    with pytest.raises(ValidationError):
        TurnCommand.model_validate(
            command.model_dump() | {"history": [{"role": "user", "content": "raw"}]}
        )

    started = await start_attempt(fixture, command)
    assert started.status == "started"
    assert started.run_id is not None
    assert started.turn is None
    attempt = fixture.sessions.documents[str(fixture.parent.id)]["last_attempt"]
    assert set(attempt) == {
        "request_id",
        "request_hash",
        "expected_version",
        "run_id",
        "process_id",
        "status",
        "started_at",
        "updated_at",
        "error_category",
    }
    assert attempt["status"] == "processing"
    assert attempt["expected_version"] == 0
    assert re.fullmatch(r"[0-9a-f]{64}", attempt["request_hash"])
    attempt_json = json.dumps(attempt, default=str)
    assert command.message not in attempt_json
    assert "history" not in attempt_json
    assert "new_items" not in attempt_json
    assert fixture.turns.documents == {}


@pytest.mark.asyncio
async def test_identical_committed_retry_replays_after_authorization_without_new_write():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    committed = await commit_attempt(fixture, command, started)
    turns_before = deepcopy(fixture.turns.documents)
    audits_before = deepcopy(fixture.audits.documents)
    auth_calls_before = len(fixture.auth_calls)
    writes_before = sum(
        len(collection.writes) for collection in fixture.database.collections.values()
    )

    replay = await fixture.repository.commit_completed_turn(
        fixture.decision(),
        fixture.parent.id,
        command,
        fixture.built_response(command),
        started.run_id,
    )
    begin_replay = await fixture.repository.begin_turn_attempt(
        fixture.decision(), fixture.parent.id, command
    )

    assert committed.status == "committed"
    assert committed.turn is not None
    assert committed.replayed is False
    assert replay.status == "committed"
    assert replay.replayed is True
    assert replay.turn == committed.turn
    assert begin_replay.status == "committed"
    assert begin_replay.turn == committed.turn
    assert fixture.turns.documents == turns_before
    assert fixture.audits.documents == audits_before
    assert (
        sum(
            len(collection.writes)
            for collection in fixture.database.collections.values()
        )
        == writes_before
    )
    assert len(fixture.auth_calls) > auth_calls_before


@pytest.mark.asyncio
async def test_changed_payload_and_stale_version_conflict_without_duplicate_turn():
    fixture = TurnFixture()
    await fixture.create_parent()
    original = fixture.command()
    started = await start_attempt(fixture, original)
    result = await commit_attempt(fixture, original, started)
    assert result.status == "committed"

    changed = fixture.command(
        request_id=original.request_id,
        expected_version=original.expected_version,
        message="Show a different question",
    )
    with pytest.raises(AppWriteConflict):
        await fixture.repository.begin_turn_attempt(
            fixture.decision(), fixture.parent.id, changed
        )

    stale = fixture.command(expected_version=0, message="A new stale request")
    with pytest.raises(AppWriteConflict):
        await fixture.repository.begin_turn_attempt(
            fixture.decision(), fixture.parent.id, stale
        )

    assert len(fixture.turns.documents) == 1
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 1


@pytest.mark.asyncio
async def test_first_turn_is_complete_owned_and_versioned_with_exact_size_and_expiry():
    expiry = NOW + timedelta(days=30)
    fixture = TurnFixture(expires_at=expiry)
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    result = await commit_attempt(fixture, command, started)

    assert result.status == "committed"
    turn = only_record(fixture.turns)
    parent = fixture.sessions.documents[str(fixture.parent.id)]
    assert turn["owner_user_id"] == str(fixture.saved.owner_id)
    assert turn["session_id"] == str(fixture.parent.id)
    assert turn["request_id"] == str(command.request_id)
    assert turn["request_hash"] == parent["last_attempt"]["request_hash"]
    assert turn["sequence"] == 1
    assert turn["user_message"] == command.message
    assert turn["response"]["status"] == "complete"
    assert turn["response"]["facts"][0]["value"] == 3
    assert turn["policy_version"] == POLICY_VERSION
    assert turn["metric_versions"] == [{"metric_id": "total_leads", "version": 1}]
    assert turn["expires_at"] == expiry
    assert type(turn["size_bytes"]) is int
    assert turn["size_bytes"] == validate_bson_document(turn)
    assert parent["version"] == 1
    assert parent["last_attempt"]["status"] == "committed"
    assert (
        fixture.database["app_settings"].documents["access_control"]["commit_fence"]
        == 1
    )
    assert len(fixture.audits.documents) == 1
    audit_text = json.dumps(only_record(fixture.audits), default=str)
    assert command.message not in audit_text
    assert "Total leads: 3" not in audit_text
    assert fixture.events.count("transaction-commit") >= 2

    record = result.turn
    assert isinstance(record, ChatTurnRecord)
    with pytest.raises(ValidationError):
        ChatTurnRecord.model_validate(
            record.model_dump() | {"new_items": [{"raw": "sdk"}]}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["turn_insert", "parent_update", "audit_insert"])
async def test_turn_commit_failures_leave_no_partial_turn_parent_or_audit(failure):
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    before_parent = deepcopy(fixture.sessions.documents[str(fixture.parent.id)])
    before_fence = fixture.database["app_settings"].documents["access_control"][
        "commit_fence"
    ]
    if failure == "turn_insert":
        fixture.turns.fail_insert = True
    elif failure == "parent_update":
        fixture.sessions.fail_update = True
    else:
        fixture.audits.fail_insert = True

    outcome = None
    try:
        outcome = await commit_attempt(fixture, command, started)
    except (AppDatabaseUnavailable, AppWriteConflict):
        pass

    assert outcome is None or outcome.status == "unknown"
    assert fixture.turns.documents == {}
    assert (
        fixture.sessions.documents[str(fixture.parent.id)]["version"]
        == before_parent["version"]
    )
    assert (
        fixture.database["app_settings"].documents["access_control"]["commit_fence"]
        == before_fence
    )
    assert fixture.audits.documents == {}
    assert "transaction-abort" in fixture.events


@pytest.mark.asyncio
async def test_duplicate_concurrent_attempts_share_one_run_and_one_committed_turn():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()

    attempt_gate = TwoPartyBarrier()
    fixture.sessions.before_find_one = lambda query: (
        attempt_gate.wait_for_two()
        if query.get("_id") == str(fixture.parent.id)
        else None
    )
    first_task = asyncio.create_task(
        fixture.repository.begin_turn_attempt(
            fixture.decision(), fixture.parent.id, command
        )
    )
    second_task = asyncio.create_task(
        fixture.repository.begin_turn_attempt(
            fixture.decision(), fixture.parent.id, command
        )
    )
    await attempt_gate.ready.wait()
    attempt_gate.release.set()
    first, second = await asyncio.gather(first_task, second_task)
    fixture.sessions.before_find_one = None
    assert {first.status, second.status} == {"started", "processing"}
    assert first.run_id == second.run_id
    started = first if first.status == "started" else second

    commit_gate = TwoPartyBarrier()
    fixture.turns.before_find_one = lambda query: (
        commit_gate.wait_for_two()
        if query.get("request_id") == str(command.request_id)
        else None
    )
    first_commit = asyncio.create_task(
        fixture.repository.commit_completed_turn(
            fixture.decision(),
            fixture.parent.id,
            command,
            fixture.built_response(command),
            started.run_id,
        )
    )
    second_commit = asyncio.create_task(
        fixture.repository.commit_completed_turn(
            fixture.decision(),
            fixture.parent.id,
            command,
            fixture.built_response(command),
            started.run_id,
        )
    )
    await commit_gate.ready.wait()
    commit_gate.release.set()
    results = await asyncio.gather(first_commit, second_commit)
    assert [result.status for result in results] == ["committed", "committed"]
    assert sum(not result.replayed for result in results) == 1
    assert len(fixture.turns.documents) == 1
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupted", "expected_status"),
    [(False, "failed"), (True, "interrupted")],
)
async def test_attempt_failure_and_restart_recovery_allow_only_same_request_retry(
    interrupted, expected_status
):
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    await fixture.repository.fail_turn_attempt(
        fixture.decision(),
        fixture.parent.id,
        command,
        started.run_id,
        "model_error",
        interrupted=interrupted,
    )
    restarted_repository = fixture.new_repository()
    recovered = await restarted_repository.resolve_turn_request(
        fixture.decision(), fixture.parent.id, command
    )

    assert recovered.status == "retry_allowed"
    assert recovered.turn is None
    assert fixture.turns.documents == {}
    assert (
        fixture.sessions.documents[str(fixture.parent.id)]["last_attempt"]["status"]
        == expected_status
    )

    retried = await restarted_repository.begin_turn_attempt(
        fixture.decision(), fixture.parent.id, command
    )
    assert retried.status == "started"
    assert retried.run_id != started.run_id
    assert command.message not in json.dumps(
        fixture.sessions.documents[str(fixture.parent.id)]["last_attempt"],
        default=str,
    )


@pytest.mark.asyncio
async def test_processing_recovery_distinguishes_restart_from_same_process_unknown(
    monkeypatch,
):
    restarted_fixture = TurnFixture()
    await restarted_fixture.create_parent()
    restarted_command = restarted_fixture.command()
    restarted_attempt = await start_attempt(restarted_fixture, restarted_command)
    monkeypatch.setattr(chat_repository_module, "_PROCESS_ID", uuid4())
    monkeypatch.setattr(chat_repository_module, "_ACTIVE_RUNS", set())
    restarted_repository = restarted_fixture.new_repository()

    restarted = await restarted_repository.resolve_turn_request(
        restarted_fixture.decision(),
        restarted_fixture.parent.id,
        restarted_command,
    )

    assert restarted.status == "retry_allowed"
    assert restarted.turn is None
    assert (
        restarted_fixture.sessions.documents[str(restarted_fixture.parent.id)][
            "last_attempt"
        ]["status"]
        == "interrupted"
    )
    retried = await restarted_repository.begin_turn_attempt(
        restarted_fixture.decision(),
        restarted_fixture.parent.id,
        restarted_command,
    )
    assert retried.status == "started"
    assert retried.run_id != restarted_attempt.run_id

    same_process_fixture = TurnFixture()
    await same_process_fixture.create_parent()
    same_process_command = same_process_fixture.command()
    same_process_attempt = await start_attempt(
        same_process_fixture, same_process_command
    )
    same_process_fixture.repository._active_runs.discard(same_process_attempt.run_id)

    unknown = await same_process_fixture.repository.resolve_turn_request(
        same_process_fixture.decision(),
        same_process_fixture.parent.id,
        same_process_command,
    )

    assert unknown.status == "unknown"
    assert unknown.turn is None
    assert (
        same_process_fixture.sessions.documents[str(same_process_fixture.parent.id)][
            "last_attempt"
        ]["status"]
        == "unknown"
    )
    blocked = await same_process_fixture.repository.begin_turn_attempt(
        same_process_fixture.decision(),
        same_process_fixture.parent.id,
        same_process_command,
    )
    assert blocked.status == "unknown"
    assert blocked.run_id is None
    assert same_process_fixture.turns.documents == {}


@pytest.mark.asyncio
async def test_authorization_and_policy_revocation_block_attempt_replay_and_commit():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    fixture.auth_error = AuthorizationDenied("revoked")
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.begin_turn_attempt(
            fixture.decision(), fixture.parent.id, command
        )
    assert fixture.sessions.documents[str(fixture.parent.id)]["last_attempt"] is None

    fixture.auth_error = None
    started = await start_attempt(fixture, command)
    fixture.database["app_settings"].documents["access_control"]["policy_version"] += 1
    with pytest.raises(AppWriteConflict):
        await fixture.repository.commit_completed_turn(
            fixture.decision(),
            fixture.parent.id,
            command,
            fixture.built_response(command),
            started.run_id,
        )
    assert fixture.turns.documents == {}
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 0
    assert fixture.audits.documents == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_binding", ["login", "role"])
async def test_revoked_login_or_role_blocks_saved_turn_commit(revoked_binding):
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    if revoked_binding == "login":
        fixture.database["login_sessions"].documents[str(fixture.saved.login.id)][
            "revoked_at"
        ] = NOW
    else:
        fixture.database["roles"].documents[str(fixture.saved.role.id)]["active"] = (
            False
        )

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.commit_completed_turn(
            fixture.decision(),
            fixture.parent.id,
            command,
            fixture.built_response(command),
            started.run_id,
        )
    assert fixture.turns.documents == {}
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 0
    assert fixture.audits.documents == {}


@pytest.mark.asyncio
async def test_revoked_access_denies_identical_committed_replay_without_returning_response():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    await commit_attempt(fixture, command, started)
    persisted_turns = deepcopy(fixture.turns.documents)
    fixture.auth_error = AuthorizationDenied("revoked")

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.resolve_turn_request(
            fixture.decision(), fixture.parent.id, command
        )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.commit_completed_turn(
            fixture.decision(),
            fixture.parent.id,
            command,
            fixture.built_response(command),
            started.run_id,
        )

    assert fixture.turns.documents == persisted_turns
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 1


@pytest.mark.asyncio
async def test_owner_scoping_does_not_return_another_users_committed_turn():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    await commit_attempt(fixture, command, started)

    other = SavedChatFixture(owner_id=uuid4())
    fixture.database["users"].documents[str(other.user.id)] = other.user.document()
    fixture.database["roles"].documents[str(other.role.id)] = other.role.document()
    fixture.database["login_sessions"].documents[str(other.login.id)] = (
        other.login.document()
    )
    other_repository = ChatRepository(
        fixture.saved.app_database,
        fixture.saved.codec,
        clock=lambda: NOW,
        authorization_recheck=fixture.authorization_recheck,
    )
    try:
        recovered = await other_repository.resolve_turn_request(
            other.decision(), fixture.parent.id, command
        )
    except (AppDatabaseUnavailable, AuthorizationDenied):
        return
    assert isinstance(recovered, TurnRecoveryResult)
    assert recovered.turn is None
    assert recovered.status != "committed"


@pytest.mark.asyncio
async def test_unknown_commit_outcome_is_reconciled_from_persisted_turn_without_new_work():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    fixture.make_next_commit_unknown()
    calls_before = len(fixture.auth_calls)

    try:
        commit = await commit_attempt(fixture, command, started)
    except (AppDatabaseUnavailable, AppWriteConflict):
        commit = None

    recovered = await fixture.repository.resolve_turn_request(
        fixture.decision(), fixture.parent.id, command
    )

    assert commit is None or commit.status in {"committed", "unknown"}
    assert recovered.status == "committed"
    assert recovered.turn is not None
    assert len(fixture.turns.documents) == 1
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 1
    assert len(fixture.auth_calls) > calls_before


@pytest.mark.asyncio
async def test_unknown_unpersisted_commit_remains_blocked_for_recovery_not_retry():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    fixture.make_next_commit_unknown(persisted=False)

    try:
        result = await commit_attempt(fixture, command, started)
    except AppDatabaseUnavailable:
        result = None
    recovery = await fixture.repository.resolve_turn_request(
        fixture.decision(), fixture.parent.id, command
    )

    assert result is None or result.status == "unknown"
    assert recovery.status in {"processing", "unknown"}
    assert recovery.turn is None
    assert fixture.turns.documents == {}
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 0
    retry = await fixture.repository.begin_turn_attempt(
        fixture.decision(), fixture.parent.id, command
    )
    assert retry.status in {"processing", "unknown"}
    assert retry.status != "started"


@pytest.mark.asyncio
async def test_commit_does_not_link_turn_to_a_stale_parent_expiry():
    old_expiry = NOW + timedelta(days=15)
    new_expiry = NOW + timedelta(days=45)
    fixture = TurnFixture(expires_at=old_expiry)
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)

    def change_expiry_before_parent_update(query, _update):
        if query.get("_id") != str(fixture.parent.id):
            return
        fixture.sessions.documents[str(fixture.parent.id)]["expires_at"] = new_expiry

    fixture.sessions.before_update_one = change_expiry_before_parent_update
    try:
        result = await commit_attempt(fixture, command, started)
    except (AppDatabaseUnavailable, AppWriteConflict, AuthorizationDenied):
        result = None

    parent = fixture.sessions.documents[str(fixture.parent.id)]
    if result is not None and result.status == "committed":
        assert only_record(fixture.turns)["expires_at"] == parent["expires_at"]
    else:
        assert fixture.turns.documents == {}
        assert parent["version"] == 0


@pytest.mark.asyncio
async def test_turn_indexes_are_only_the_two_approved_unique_app_indexes():
    fixture = TurnFixture()

    await fixture.saved.app_database.initialize_turn_indexes()

    assert fixture.turns.indexes == [
        (
            (("session_id", 1), ("request_id", 1)),
            {"unique": True, "name": "uq_session_request"},
        ),
        (
            (("session_id", 1), ("sequence", 1)),
            {"unique": True, "name": "uq_session_sequence"},
        ),
    ]
    assert set(fixture.database.collections) <= {
        "app_settings",
        "users",
        "roles",
        "chat_sessions",
        "chat_turns",
        "audit_events",
        "login_sessions",
    }


@pytest.mark.asyncio
async def test_turn_index_failure_returns_safe_error_without_leaking_driver_text():
    fixture = TurnFixture()
    fixture.turns.fail_index = True

    with pytest.raises(
        AppDatabaseUnavailable, match="^Saved-turn indexes are unavailable\\.$"
    ) as error:
        await fixture.saved.app_database.initialize_turn_indexes()

    assert "synthetic index failure" not in str(error.value)
    assert fixture.turns.indexes == []


@pytest.mark.asyncio
async def test_missing_authorization_recheck_denies_every_turn_operation_but_keeps_chat_creation():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    repository_without_recheck = ChatRepository(
        fixture.saved.app_database,
        fixture.saved.codec,
        clock=lambda: NOW,
    )

    created = await repository_without_recheck.create_saved_chat(
        fixture.decision(), uuid4()
    )
    assert created.version == 0

    operations = (
        repository_without_recheck.begin_turn_attempt(
            fixture.decision(), fixture.parent.id, command
        ),
        repository_without_recheck.fail_turn_attempt(
            fixture.decision(),
            fixture.parent.id,
            command,
            uuid4(),
            "model_error",
        ),
        repository_without_recheck.commit_completed_turn(
            fixture.decision(),
            fixture.parent.id,
            command,
            fixture.built_response(command),
            uuid4(),
        ),
        repository_without_recheck.resolve_turn_request(
            fixture.decision(), fixture.parent.id, command
        ),
    )
    for operation in operations:
        with pytest.raises(AuthorizationDenied):
            await operation

    assert fixture.sessions.documents[str(fixture.parent.id)]["last_attempt"] is None
    assert fixture.turns.documents == {}
    assert fixture.audits.documents == {}


@pytest.mark.asyncio
async def test_follow_up_state_survives_same_policy_notice_and_clears_after_policy_change():
    fixture = TurnFixture()
    await fixture.create_parent()

    first_command = fixture.command(expected_version=0, message="Show leads")
    first_attempt = await start_attempt(fixture, first_command)
    first_commit = await commit_attempt(fixture, first_command, first_attempt)
    first_state = first_commit.turn.question_state_after
    assert first_state is not None
    assert first_state.through_sequence == 1
    assert (
        fixture.sessions.documents[str(fixture.parent.id)]["context_state"][
            "through_sequence"
        ]
        == 1
    )

    notice_command = fixture.command(
        expected_version=1, message="Clarify the last result"
    )
    notice_attempt = await fixture.repository.begin_turn_attempt(
        fixture.decision(), fixture.parent.id, notice_command
    )
    notice_commit = await fixture.repository.commit_completed_turn(
        fixture.decision(),
        fixture.parent.id,
        notice_command,
        notice_response(notice_command, POLICY_VERSION),
        notice_attempt.run_id,
    )
    assert notice_commit.turn.question_state_after == first_state
    assert notice_commit.turn.question_state_after.through_sequence == 1
    assert (
        fixture.sessions.documents[str(fixture.parent.id)]["context_state"][
            "through_sequence"
        ]
        == 1
    )

    next_policy_version = POLICY_VERSION + 1
    fixture.database["app_settings"].documents["access_control"]["policy_version"] = (
        next_policy_version
    )
    changed_decision = decision_at_policy_version(
        fixture.decision(), next_policy_version
    )
    changed_command = fixture.command(
        expected_version=2, message="Ask after policy changed"
    )
    changed_attempt = await fixture.repository.begin_turn_attempt(
        changed_decision, fixture.parent.id, changed_command
    )
    changed_commit = await fixture.repository.commit_completed_turn(
        changed_decision,
        fixture.parent.id,
        changed_command,
        notice_response(changed_command, next_policy_version),
        changed_attempt.run_id,
    )

    assert changed_commit.turn.question_state_after is None
    assert fixture.sessions.documents[str(fixture.parent.id)]["context_state"] is None


@pytest.mark.asyncio
async def test_committed_turn_is_not_released_after_policy_version_changes():
    fixture = TurnFixture()
    await fixture.create_parent()
    command = fixture.command()
    started = await start_attempt(fixture, command)
    committed = await commit_attempt(fixture, command, started)
    assert committed.status == "committed"
    assert committed.turn.policy_version == POLICY_VERSION

    next_policy_version = POLICY_VERSION + 1
    fixture.database["app_settings"].documents["access_control"]["policy_version"] = (
        next_policy_version
    )
    current_decision = decision_at_policy_version(
        fixture.decision(), next_policy_version
    )
    turns_before = deepcopy(fixture.turns.documents)
    audits_before = deepcopy(fixture.audits.documents)

    with pytest.raises(AuthorizationDenied):
        await fixture.repository.resolve_turn_request(
            current_decision, fixture.parent.id, command
        )
    with pytest.raises(AuthorizationDenied):
        await fixture.repository.commit_completed_turn(
            current_decision,
            fixture.parent.id,
            command,
            notice_response(command, next_policy_version),
            started.run_id,
        )

    assert fixture.turns.documents == turns_before
    assert fixture.audits.documents == audits_before
    assert fixture.sessions.documents[str(fixture.parent.id)]["version"] == 1
