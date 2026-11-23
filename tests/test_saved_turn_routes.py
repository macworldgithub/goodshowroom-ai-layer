"""Signed HTTP saved turns through the agent and fault-injected repository."""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from openai import OpenAIError

from goodshowroom_ai.agent_service import PrimaryAgentService, QuestionPlan
from goodshowroom_ai.app import create_app
from goodshowroom_ai.authorization import AuthorizationService, Principal
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.metrics import MetricRegistry
from goodshowroom_ai.normalization import LeadAdapterConfig
from goodshowroom_ai.responses import ResponseBuilder
from goodshowroom_ai.saved_turn_routes import SavedTurnAPI
from tests.test_agent_service import OfflineModel, ScriptedRunner, TraceStub
from tests.test_chat_history import HistoryCollection
from tests.test_chat_turns import NOW, TurnFixture
from tests.test_identity_bridge import _mint, _request_claims, _verifier


class SignedTurns:
    async def configure(self, signing_key):
        self.fixture = TurnFixture()
        self.fixture.database.collections["chat_turns"] = HistoryCollection(
            "chat_turns"
        )
        await self.fixture.create_parent()
        self.client_key = signing_key
        self.allowed = True
        self.other_snapshot = None
        self.model_calls = 0
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.wait_for_resume = False
        self.model_error = False
        self.selected_fault = None
        self.clock = NOW

        async def load_policy(principal):
            if self.other_snapshot and principal.user_id == self.other_snapshot.user.id:
                return self.other_snapshot
            return self.fixture.decision().snapshot

        async def current(principal):
            snapshot = await load_policy(principal)
            return self.allowed and principal == Principal(
                user_id=snapshot.user.id,
                login_id=snapshot.login.id,
                generation=snapshot.login.generation,
            )

        self.authorization = AuthorizationService(
            load_policy=load_policy, check_identity=current, clock=lambda: self.clock
        )

        async def no_unreviewed_history(_decision, _turn):
            return False

        repository = self.fixture.repository
        repository._authorization_recheck = self.authorization.recheck
        repository._turn_access_check = no_unreviewed_history
        repository._clock = lambda: self.clock
        original_commit = repository.commit_completed_turn

        async def commit_with_fault(*args):
            if self.selected_fault is not None:
                self.fixture.make_next_commit_unknown(persisted=self.selected_fault)
            return await original_commit(*args)

        repository.commit_completed_turn = commit_with_fault
        config = LeadAdapterConfig(
            binding="lead_records", site_columns=("dealer", "dealerName")
        )
        agent = PrimaryAgentService(
            authorization=self.authorization,
            responses=ResponseBuilder(
                disclosure=None, authorization=self.authorization, config=config
            ),
            repository=repository,
            config=config,
            registry=MetricRegistry(),
            tracing=TraceStub(),
            model=OfflineModel(),
            runner=self.run,
        )
        self.api = SavedTurnAPI(
            self.fixture.saved.app_database,
            _verifier(signing_key),
            agent,
            lambda: self.clock,
        )
        self.app = create_app(Settings(), saved_turns=self.api)
        self.turn_path = f"/v1/saved-chats/{self.fixture.parent.id}/turns"
        self.payload = {
            "request_id": str(uuid4()),
            "expected_version": 0,
            "message": "Synthetic private message",
        }

    async def run(self, *args, **kwargs):
        self.model_calls += 1
        self.entered.set()
        if self.wait_for_resume:
            await self.resume.wait()
        if self.model_error:
            raise OpenAIError("synthetic private diagnostic")
        return await ScriptedRunner(QuestionPlan(intent="unsupported"))(*args, **kwargs)

    def recovery(self, request_id=None):
        return f"/v1/saved-chats/{self.fixture.parent.id}/requests/{request_id or self.payload['request_id']}"

    def request(self, method, path, payload=None, *, sign_body=None):
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        decision = self.fixture.decision()
        stamp = int(self.clock.timestamp())
        claims = _request_claims(
            sub=str(decision.principal.user_id),
            login_session_id=str(decision.principal.login_id),
            generation=decision.principal.generation,
            iat=stamp,
            nbf=stamp,
            exp=stamp + 90,
            method=method,
            path=path,
            digest=operation_digest(
                method, path, body=body if sign_body is None else sign_body
            ),
        )
        token = _mint(self.client_key, claims)
        return body, {"Authorization": f"Bearer {token}"}

    async def send(self, method, path, payload=None, *, sign_body=None):
        body, headers = self.request(method, path, payload, sign_body=sign_body)
        async with AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test.invalid"
        ) as client:
            return await client.request(method, path, content=body, headers=headers)

    async def raw_post(self, *, disconnect=False, lose_response=False):
        body, headers = self.request("POST", self.turn_path, self.payload)
        first = True
        sent = []

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            await self.entered.wait()
            if not disconnect:
                await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if lose_response:
                raise OSError("synthetic private transport diagnostic")
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "POST",
            "path": self.turn_path,
            "raw_path": self.turn_path.encode(),
            "query_string": b"",
            "root_path": "",
            "server": ("test.invalid", 80),
            "client": ("127.0.0.1", 41000),
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
        }
        await self.app(scope, receive, send)
        return sent


@pytest_asyncio.fixture
async def signed(signing_key):
    case = SignedTurns()
    await case.configure(signing_key)
    return case


@pytest.mark.asyncio
async def test_lost_response_recovery_rechecks_access_and_replays_without_model(signed):
    with pytest.raises(RuntimeError, match="Response transmission failed"):
        await signed.raw_post(lose_response=True)
    observations = []
    for _ in range(2):
        result = await signed.send("GET", signed.recovery())
        assert result.status_code == 200, result.text
        observations.append(result.json()["data"])
    assert observations[0] == observations[1]
    committed = observations[0]
    assert committed["status"] == "committed"
    assert committed["result"]["persistence"] == "committed"
    assert signed.payload["message"] not in result.text
    replay = await signed.send("POST", signed.turn_path, signed.payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == committed["result"]
    changed = await signed.send(
        "POST", signed.turn_path, {**signed.payload, "expected_version": 1}
    )
    assert (
        changed.status_code == 409
        and changed.json()["error"]["code"] == "idempotency_conflict"
    )
    stale = await signed.send(
        "POST", signed.turn_path, {**signed.payload, "request_id": str(uuid4())}
    )
    assert (
        stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    )
    signed.allowed = False
    denied = await signed.send("GET", signed.recovery())
    assert denied.status_code == 403 and "answer" not in denied.text
    assert signed.model_calls == len(signed.fixture.turns.documents) == 1


@pytest.mark.asyncio
async def test_live_duplicate_and_changed_content_are_distinguished(signed):
    signed.wait_for_resume = True
    pending = asyncio.create_task(signed.send("POST", signed.turn_path, signed.payload))
    try:
        await asyncio.wait_for(signed.entered.wait(), 3)
        status = await signed.send("GET", signed.recovery())
        assert status.json()["data"] == {"status": "processing", "result": None}
        identical = await signed.send("POST", signed.turn_path, signed.payload)
        assert identical.status_code == 409 and identical.headers["Retry-After"] == "1"
        assert identical.json()["error"]["code"] == "request_in_progress"
        different = await signed.send(
            "POST", signed.turn_path, {**signed.payload, "message": "Changed"}
        )
        assert (
            different.status_code == 409
            and different.json()["error"]["code"] == "idempotency_conflict"
        )
    finally:
        signed.resume.set()
        finish = await pending
    assert finish.status_code == 200 and signed.model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [True, False])
async def test_uncertain_save_returns_only_currently_reconciled_result(
    signed, persisted
):
    signed.selected_fault = persisted
    posted = await signed.send("POST", signed.turn_path, signed.payload)
    if persisted:
        assert posted.status_code == 200, posted.text
        assert posted.json()["data"]["persistence"] == "committed"
    else:
        assert posted.status_code == 503, posted.text
        assert posted.json()["error"]["commit_state"] == "unknown"
        assert posted.json()["request_id"] == signed.payload["request_id"]
        assert (
            await signed.send("POST", signed.turn_path, signed.payload)
        ).status_code == 503
    status = (await signed.send("GET", signed.recovery())).json()["data"]
    assert status["status"] == ("committed" if persisted else "unknown")
    assert (status["result"] is not None) is persisted
    assert len(signed.fixture.turns.documents) == int(persisted)
    assert signed.model_calls == 1


@pytest.mark.asyncio
async def test_disconnect_and_model_failure_produce_safe_terminal_metadata(signed):
    signed.wait_for_resume = True
    sent = await asyncio.wait_for(signed.raw_post(disconnect=True), 3)
    assert sent[0]["status"] == 499
    proof = await signed.send("GET", signed.recovery())
    assert proof.json()["data"] == {"status": "interrupted", "result": None}
    signed.wait_for_resume = False
    signed.model_error = True
    failed = await signed.send("POST", signed.turn_path, signed.payload)
    assert (
        failed.status_code == 502 and failed.json()["error"]["code"] == "model_failure"
    )
    assert "synthetic private" not in failed.text
    assert (await signed.send("GET", signed.recovery())).json()["data"] == {
        "status": "failed",
        "result": None,
    }
    assert not signed.fixture.turns.documents


@pytest.mark.asyncio
async def test_mid_run_revocation_denies_commit_and_mismatched_signature_denies_execution(
    signed,
):
    bad = await signed.send("POST", signed.turn_path, signed.payload, sign_body=b"{}")
    assert bad.status_code == 401 and signed.model_calls == 0
    signed.wait_for_resume = True
    pending = asyncio.create_task(signed.send("POST", signed.turn_path, signed.payload))
    try:
        await asyncio.wait_for(signed.entered.wait(), 3)
        signed.allowed = False
    finally:
        signed.resume.set()
        denied = await pending
    assert denied.status_code == 403
    assert not signed.fixture.turns.documents
    assert (await signed.send("GET", signed.recovery())).status_code == 403


@pytest.mark.asyncio
async def test_expired_login_and_unknown_request_do_not_release_content(signed):
    assert (await signed.send("GET", signed.recovery())).status_code == 404
    signed.clock = NOW + timedelta(hours=9)
    denied = await signed.send("GET", signed.recovery())
    assert denied.status_code == 401
    assert signed.model_calls == 0


@pytest.mark.asyncio
async def test_another_approved_owner_cannot_execute_or_recover_a_saved_turn(signed):
    assert (
        await signed.send("POST", signed.turn_path, signed.payload)
    ).status_code == 200
    original = signed.fixture.decision().snapshot
    user = original.user.model_copy(update={"id": uuid4()})
    login = original.login.model_copy(update={"id": uuid4(), "user_id": user.id})
    signed.fixture.database["users"].documents[str(user.id)] = user.document()
    signed.fixture.database["login_sessions"].documents[str(login.id)] = (
        login.document()
    )
    signed.other_snapshot = original.model_copy(update={"user": user, "login": login})
    for method, path in (("GET", signed.recovery()), ("POST", signed.turn_path)):
        body = b"" if method == "GET" else json.dumps(signed.payload).encode()
        stamp = int(NOW.timestamp())
        token = _mint(
            signed.client_key,
            _request_claims(
                sub=str(user.id),
                login_session_id=str(login.id),
                generation=login.generation,
                iat=stamp,
                nbf=stamp,
                exp=stamp + 90,
                method=method,
                path=path,
                digest=operation_digest(method, path, body=body),
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=signed.app), base_url="http://test.invalid"
        ) as client:
            result = await client.request(
                method, path, content=body, headers={"Authorization": f"Bearer {token}"}
            )
        assert result.status_code == 403
        assert "answer" not in result.text
    assert signed.model_calls == 1
