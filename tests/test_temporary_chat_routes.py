"""TICKET-039: signed, memory-only temporary chat routes.

The tests use signed HTTP, a fake clock, injected model failures and in-flight
cancellation. Storage and log sentinels prove nothing is saved or logged.
"""

import asyncio
import json
import logging
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
from goodshowroom_ai.temporary import TemporaryMemoryStore
from goodshowroom_ai.temporary_chat_routes import build_temporary_chat_api
from tests.test_agent_service import OfflineModel, ScriptedRunner, TraceStub
from tests.test_chat_history import HistoryCollection
from tests.test_chat_turns import NOW, TurnFixture
from tests.test_identity_bridge import _mint, _request_claims, _verifier

SECRET_TEXT = "TEMPORARY-ROUTE-SENTINEL-039"


class SignedTemporary:
    async def configure(self, signing_key):
        self.fixture = TurnFixture()
        self.fixture.database.collections["chat_turns"] = HistoryCollection(
            "chat_turns"
        )
        self.client_key = signing_key
        self.allowed = True
        self.other_snapshot = None
        self.model_calls = 0
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.wait_for_resume = False
        self.model_error = False
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
        self.store = TemporaryMemoryStore(
            authorization=self.authorization, clock=lambda: self.clock
        )
        config = LeadAdapterConfig(
            binding="lead_records", site_columns=("dealer", "dealerName")
        )

        async def no_history(_decision, _turn):
            return False

        repository = self.fixture.repository
        repository._authorization_recheck = self.authorization.recheck
        repository._turn_access_check = no_history
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
            memory=self.store,
        )
        self.api = build_temporary_chat_api(
            self.fixture.saved.app_database,
            _verifier(signing_key),
            lambda: self.clock,
            agent=agent,
            memory=self.store,
        )
        self.app = create_app(Settings(), temporary_chats=self.api)
        self.baseline = self.write_count()

    async def run(self, *args, **kwargs):
        self.model_calls += 1
        self.entered.set()
        if self.wait_for_resume:
            await self.resume.wait()
        if self.model_error:
            raise OpenAIError(f"synthetic {SECRET_TEXT} diagnostic")
        return await ScriptedRunner(QuestionPlan(intent="unsupported"))(*args, **kwargs)

    def write_count(self):
        return sum(
            len(getattr(collection, "writes", ()))
            for collection in self.fixture.database.collections.values()
        )

    def stored_text(self):
        return repr(
            {
                name: getattr(collection, "documents", {})
                for name, collection in self.fixture.database.collections.items()
            }
        )

    def sign(self, method, path, body, *, snapshot=None):
        snapshot = snapshot or self.fixture.decision().snapshot
        stamp = int(self.clock.timestamp())
        claims = _request_claims(
            sub=str(snapshot.user.id),
            login_session_id=str(snapshot.login.id),
            generation=snapshot.login.generation,
            iat=stamp,
            nbf=stamp,
            exp=stamp + 90,
            method=method,
            path=path,
            digest=operation_digest(method, path, body=body),
        )
        return {"Authorization": f"Bearer {_mint(self.client_key, claims)}"}

    async def send(self, method, path, payload=None, *, snapshot=None, sign_body=None):
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        headers = self.sign(
            method, path, body if sign_body is None else sign_body, snapshot=snapshot
        )
        async with AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test.invalid"
        ) as client:
            return await client.request(method, path, content=body, headers=headers)

    async def create(self, **kwargs):
        return await self.send(
            "POST", "/v1/temporary-chats", {"request_id": str(uuid4())}, **kwargs
        )

    async def new_chat(self):
        response = await self.create()
        assert response.status_code == 201
        return response.json()["data"]["id"]

    def turn(self, message=SECRET_TEXT, version=0, request_id=None):
        return {
            "request_id": request_id or str(uuid4()),
            "expected_version": version,
            "message": message,
        }

    def renew_login(self, at):
        """Move the clock and renew the login lease, as the web server would."""
        self.clock = at
        login = self.fixture.saved.login.model_copy(
            update={"updated_at": at, "lease_valid_until": at + timedelta(minutes=5)}
        )
        saved = self.fixture.saved
        saved.login = login
        saved.snapshot = saved.snapshot.model_copy(update={"login": login})
        self.fixture.database["login_sessions"].documents[str(login.id)] = (
            login.document()
        )

    def second_user(self):
        original = self.fixture.decision().snapshot
        user = original.user.model_copy(update={"id": uuid4()})
        login = original.login.model_copy(update={"id": uuid4(), "user_id": user.id})
        self.fixture.database["users"].documents[str(user.id)] = user.document()
        self.fixture.database["login_sessions"].documents[str(login.id)] = (
            login.document()
        )
        self.other_snapshot = original.model_copy(update={"user": user, "login": login})
        return self.other_snapshot


@pytest_asyncio.fixture
async def temp(signing_key):
    case = SignedTemporary()
    await case.configure(signing_key)
    return case


def code(response):
    return response.json()["error"]["code"]


@pytest.mark.asyncio
async def test_create_recovers_the_same_chat_and_reports_not_saved(temp):
    request_id = str(uuid4())
    first = await temp.send("POST", "/v1/temporary-chats", {"request_id": request_id})
    again = await temp.send("POST", "/v1/temporary-chats", {"request_id": request_id})
    other = await temp.create()
    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 201)
    assert first.json()["data"] == again.json()["data"]
    assert first.json()["data"]["mode"] == "temporary"
    assert first.json()["data"]["version"] == 0
    assert first.json()["data"]["persistence"] == "not_saved"
    assert other.json()["data"]["id"] != first.json()["data"]["id"]
    assert first.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_success_advances_only_memory_and_replays_without_the_model(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    payload = temp.turn()
    first = await temp.send("POST", path, payload)
    assert first.status_code == 200
    data = first.json()["data"]
    assert (data["mode"], data["persistence"], data["version"]) == (
        "temporary",
        "not_saved",
        1,
    )
    assert data["session_id"] == chat
    replay = await temp.send("POST", path, payload)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert temp.model_calls == 1
    changed = await temp.send("POST", path, {**payload, "message": "Changed"})
    assert changed.status_code == 409 and code(changed) == "idempotency_conflict"
    stale = await temp.send("POST", path, temp.turn(version=0))
    assert stale.status_code == 409 and code(stale) == "version_conflict"
    follow_up = await temp.send("POST", path, temp.turn(version=1))
    assert follow_up.status_code == 200 and follow_up.json()["data"]["version"] == 2
    assert temp.model_calls == 2


@pytest.mark.asyncio
async def test_status_recovers_in_memory_states_only(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    payload = temp.turn()
    status = lambda request_id: temp.send(  # noqa: E731
        "GET", f"/v1/temporary-chats/{chat}/requests/{request_id}"
    )
    assert (await status(payload["request_id"])).status_code == 404

    temp.wait_for_resume = True
    pending = asyncio.create_task(temp.send("POST", path, payload))
    await asyncio.wait_for(temp.entered.wait(), 3)
    processing = await status(payload["request_id"])
    assert processing.json()["data"] == {"status": "processing", "result": None}
    temp.resume.set()
    assert (await pending).status_code == 200
    committed = (await status(payload["request_id"])).json()["data"]
    assert committed["status"] == "committed"
    assert committed["result"]["version"] == 1
    assert committed["result"]["persistence"] == "not_saved"

    temp.wait_for_resume, temp.model_error = False, True
    failed_payload = temp.turn(version=1)
    failed = await temp.send("POST", path, failed_payload)
    assert failed.status_code == 502 and SECRET_TEXT not in failed.text
    assert (await status(failed_payload["request_id"])).json()["data"] == {
        "status": "retry_allowed",
        "result": None,
    }
    temp.model_error = False
    ok = await temp.send("POST", path, temp.turn(version=1))
    assert ok.status_code == 200
    assert (await status(failed_payload["request_id"])).json()["data"] == {
        "status": "unknown",
        "result": None,
    }


@pytest.mark.asyncio
async def test_unknown_expired_and_cross_user_chats_look_identical(temp):
    chat = await temp.new_chat()
    outsider = temp.second_user()
    unknown = str(uuid4())
    turn = temp.turn()
    answers = []
    for target, snapshot in ((chat, outsider), (unknown, None)):
        answers.append(
            await temp.send(
                "POST",
                f"/v1/temporary-chats/{target}/turns",
                turn,
                snapshot=snapshot,
            )
        )
        answers.append(
            await temp.send(
                "GET",
                f"/v1/temporary-chats/{target}/requests/{turn['request_id']}",
                snapshot=snapshot,
            )
        )
    temp.renew_login(NOW + timedelta(minutes=31))
    answers.append(await temp.send("POST", f"/v1/temporary-chats/{chat}/turns", turn))
    assert {answer.status_code for answer in answers} == {410}
    assert {code(answer) for answer in answers} == {"temporary_unavailable"}
    assert {json.dumps(answer.json()["error"]) for answer in answers} == {
        json.dumps(answers[0].json()["error"])
    }
    assert temp.model_calls == 0


@pytest.mark.asyncio
async def test_end_is_idempotent_and_late_work_cannot_restore_the_chat(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    temp.wait_for_resume = True
    pending = asyncio.create_task(temp.send("POST", path, temp.turn()))
    await asyncio.wait_for(temp.entered.wait(), 3)
    outsider = temp.second_user()
    foreign = await temp.send(
        "DELETE", f"/v1/temporary-chats/{chat}", snapshot=outsider
    )
    assert foreign.status_code == 204  # No existence difference for a stranger.
    ended = await temp.send("DELETE", f"/v1/temporary-chats/{chat}")
    again = await temp.send("DELETE", f"/v1/temporary-chats/{chat}")
    never = await temp.send("DELETE", f"/v1/temporary-chats/{uuid4()}")
    assert [ended.status_code, again.status_code, never.status_code] == [204] * 3
    assert ended.content == b""
    temp.resume.set()
    late = await pending
    assert late.status_code in {410, 499}
    assert SECRET_TEXT not in late.text
    after = await temp.send("POST", path, temp.turn())
    assert after.status_code == 410 and code(after) == "temporary_unavailable"
    assert not temp.store._entries


@pytest.mark.asyncio
async def test_client_disconnect_cancels_work_and_frees_the_chat(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    temp.wait_for_resume = True
    body = json.dumps(temp.turn(), separators=(",", ":")).encode()
    headers = temp.sign("POST", path, body)
    first, sent = True, []

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": body, "more_body": False}
        await temp.entered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("test.invalid", 80),
        "client": ("127.0.0.1", 41000),
        "headers": [(n.lower().encode(), v.encode()) for n, v in headers.items()],
    }
    await asyncio.wait_for(temp.app(scope, receive, send), 3)
    assert sent[0]["status"] == 499
    temp.wait_for_resume = False
    entry = next(iter(temp.store._entries.values()))
    assert entry.pending is None and entry.version == 0
    assert (await temp.send("POST", path, temp.turn())).status_code == 200


@pytest.mark.asyncio
async def test_logout_revocation_and_expiry_end_the_chat_without_writes(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    assert (await temp.send("POST", path, temp.turn())).status_code == 200
    principal = temp.fixture.decision().principal
    await temp.store.invalidate_login(principal)
    denied = await temp.send("POST", path, temp.turn(version=1))
    assert denied.status_code == 410 and temp.model_calls == 1
    assert not temp.store._entries
    assert (await temp.create()).status_code == 410  # A signed-out login stays out.

    # Another login: revoking its access ends its chat, and no content is released.
    temp.second_user()
    other = temp.other_snapshot
    created = await temp.create(snapshot=other)
    assert created.status_code == 201
    other_chat = created.json()["data"]["id"]
    temp.allowed = False
    revoked = await temp.send(
        "POST", f"/v1/temporary-chats/{other_chat}/turns", temp.turn(), snapshot=other
    )
    assert revoked.status_code == 403 and "answer" not in revoked.text
    assert not temp.store._entries
    assert temp.model_calls == 1
    assert temp.write_count() == temp.baseline


@pytest.mark.asyncio
async def test_nothing_is_stored_or_logged_and_no_history_route_exists(temp, caplog):
    caplog.set_level(logging.DEBUG)
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    payload = temp.turn()
    assert (await temp.send("POST", path, payload)).status_code == 200
    temp.model_error = True
    assert (await temp.send("POST", path, temp.turn(version=1))).status_code == 502
    await temp.send(
        "GET", f"/v1/temporary-chats/{chat}/requests/{payload['request_id']}"
    )
    await temp.send("DELETE", f"/v1/temporary-chats/{chat}")
    await temp.send("POST", path, temp.turn(version=1))
    assert temp.write_count() == temp.baseline
    stored = temp.stored_text()
    # The test client logs its own URLs. Only the server's records count here.
    server_log = "\n".join(
        f"{record.name} {record.getMessage()} {record.exc_text or ''}"
        for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore", "asyncio"))
    )
    for secret in (SECRET_TEXT, chat, payload["request_id"]):
        assert secret not in stored
        assert secret not in server_log
    for method, route in (
        ("GET", "/v1/temporary-chats"),
        ("GET", f"/v1/temporary-chats/{chat}"),
        ("GET", f"/v1/temporary-chats/{chat}/turns"),
        ("GET", f"/v1/admin/temporary-chats/{chat}"),
        ("PATCH", f"/v1/temporary-chats/{chat}"),
    ):
        assert (await temp.send(method, route)).status_code in {404, 405}


@pytest.mark.asyncio
async def test_bad_requests_are_refused_before_memory_or_model(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    payload = temp.turn()
    forged = await temp.send("POST", path, payload, sign_body=b"{}")
    assert forged.status_code == 401
    async with AsyncClient(
        transport=ASGITransport(app=temp.app), base_url="http://test.invalid"
    ) as client:
        unsigned = await client.post(
            "/v1/temporary-chats", json={"request_id": str(uuid4())}
        )
        assert unsigned.status_code == 401
        query_path = f"/v1/temporary-chats/{chat}/requests/{uuid4()}?x=1"
        denied = await client.get(
            query_path, headers=temp.sign("GET", query_path.split("?")[0], b"")
        )
        assert denied.status_code == 401
    bad_id = await temp.send("POST", "/v1/temporary-chats/not-a-uuid/turns", payload)
    assert bad_id.status_code == 422
    blank = await temp.send("POST", path, temp.turn(message="   "))
    assert blank.status_code == 422
    extra = await temp.send("POST", path, {**payload, "mode": "saved"})
    assert extra.status_code == 422
    assert temp.model_calls == 0


@pytest.mark.asyncio
async def test_limits_and_missing_service_fail_safely(temp):
    for _ in range(10):
        assert (await temp.create()).status_code == 201
    limited = await temp.create()
    assert limited.status_code == 429 and code(limited) == "rate_limited"
    bare = create_app(Settings())
    temp.app = bare
    unavailable = await temp.create()
    assert (
        unavailable.status_code == 410 and code(unavailable) == "temporary_unavailable"
    )


@pytest.mark.asyncio
async def test_login_lease_expiry_denies_use_and_the_sweep_drops_memory(temp):
    chat = await temp.new_chat()
    path = f"/v1/temporary-chats/{chat}/turns"
    assert (await temp.send("POST", path, temp.turn())).status_code == 200
    temp.clock = NOW + timedelta(minutes=6)
    expired = await temp.send("POST", path, temp.turn(version=1))
    assert expired.status_code == 401 and SECRET_TEXT not in expired.text
    temp.store.sweep()
    assert not temp.store._entries
    assert temp.write_count() == temp.baseline
    assert temp.model_calls == 1
