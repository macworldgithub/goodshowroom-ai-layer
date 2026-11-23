"""Signed HTTP checks for saved-chat management using isolated fixtures."""

import json
from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.saved_chat_routes import build_saved_chat_api
from goodshowroom_ai.storage.mongo import AppDatabaseUnavailable, _AppWriteTransaction
from tests.test_chat_history import HistoryCollection
from tests.test_chat_turns import NOW
from tests.test_identity_bridge import _mint, _request_claims, _verifier
from tests.test_saved_turn_routes import SignedTurns

CHATS = "/v1/saved-chats"


class ChatRequests(SignedTurns):
    async def configure(self, signing_key):
        await super().configure(signing_key)
        sessions = HistoryCollection("chat_sessions")
        sessions.documents = self.fixture.sessions.documents
        self.fixture.database.collections["chat_sessions"] = sessions
        self.cursor_time = NOW
        self.codec = CursorCodec(
            Fernet.generate_key(), ttl_seconds=30, clock=lambda: self.cursor_time
        )
        self.app.state.saved_chats = build_saved_chat_api(
            self.fixture.saved.app_database,
            _verifier(signing_key),
            self.authorization,
            lambda: self.clock,
            self.codec,
        )

    async def manage(
        self,
        method,
        path=CHATS,
        *,
        payload=None,
        params=None,
        other=False,
        tamper=False,
    ):
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        query = urlencode(params or {}).encode("ascii")
        actual = query + b"&page_size=2" if tamper else query
        snapshot = self.other_snapshot if other else self.fixture.decision().snapshot
        stamp = int(self.clock.timestamp())
        token = _mint(
            self.client_key,
            _request_claims(
                sub=str(snapshot.user.id),
                login_session_id=str(snapshot.login.id),
                generation=snapshot.login.generation,
                method=method,
                path=path,
                iat=stamp,
                nbf=stamp,
                exp=stamp + 90,
                digest=operation_digest(method, path, query=query, body=body),
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test.invalid"
        ) as client:
            return await client.request(
                method,
                path + ("?" + actual.decode() if actual else ""),
                content=body,
                headers={"Authorization": f"Bearer {token}"},
            )

    def add_other_owner(self):
        original = self.fixture.decision().snapshot
        user = original.user.model_copy(update={"id": uuid4()})
        login = original.login.model_copy(update={"id": uuid4(), "user_id": user.id})
        self.fixture.database["users"].documents[str(user.id)] = user.document()
        self.fixture.database["login_sessions"].documents[str(login.id)] = (
            login.document()
        )
        self.other_snapshot = original.model_copy(update={"user": user, "login": login})


@pytest_asyncio.fixture
async def chats(signing_key):
    case = ChatRequests()
    await case.configure(signing_key)
    return case


@pytest.mark.asyncio
async def test_create_and_replay_return_distinct_statuses_bound_to_owner(chats):
    request = {"request_id": str(uuid4())}
    first = await chats.manage("POST", payload=request)
    again = await chats.manage("POST", payload=request)
    assert first.status_code == 201, first.text
    assert again.status_code == 200 and again.json() == first.json()
    expected = first.json()["data"]
    assert (
        expected["version"] == 0
        and expected["title"] == "New chat"
        and expected["mode"] == "saved"
    )
    chats.add_other_owner()
    other = await chats.manage("POST", payload=request, other=True)
    assert other.status_code == 201 and other.json()["data"]["id"] != expected["id"]
    visible = (await chats.manage("GET", other=True)).json()["data"]["chats"]
    assert [item["id"] for item in visible] == [other.json()["data"]["id"]]
    for chat_id in (expected["id"], str(uuid4())):
        result = await chats.manage("GET", f"{CHATS}/{chat_id}/turns", other=True)
        assert (
            result.status_code == 404 and result.json()["error"]["code"] == "not_found"
        )
        assert (
            await chats.manage("DELETE", f"{CHATS}/{chat_id}", other=True)
        ).status_code == 204
    assert expected["id"] in chats.fixture.sessions.documents


@pytest.mark.asyncio
async def test_list_cursor_is_signed_scoped_expiring_and_bound_to_policy(chats):
    assert (
        await chats.manage("POST", payload={"request_id": str(uuid4())})
    ).status_code == 201
    initial = await chats.manage("GET", params={"page_size": 1})
    assert initial.status_code == 200, initial.text
    data = initial.json()["data"]
    assert set(data) == {"chats", "next_cursor"}
    cursor = data["next_cursor"]
    assert cursor and len(data["chats"]) == 1
    later = await chats.manage("GET", params={"page_size": 1, "cursor": cursor})
    assert later.status_code == 200 and later.json()["data"]["chats"] != data["chats"]
    tampered = await chats.manage("GET", params={"page_size": 1, "cursor": "invalid"})
    assert tampered.status_code == 422
    bad_signature = await chats.manage("GET", params={"page_size": 1}, tamper=True)
    assert bad_signature.status_code == 401
    chats.cursor_time = NOW + timedelta(seconds=31)
    expired = await chats.manage("GET", params={"page_size": 1, "cursor": cursor})
    assert expired.status_code == 422
    chats.cursor_time = NOW
    version = chats.fixture.decision().policy_version + 1
    chats.fixture.saved.snapshot = chats.fixture.saved.snapshot.model_copy(
        update={"policy_version": version}
    )
    chats.fixture.database["app_settings"].documents["access_control"][
        "policy_version"
    ] = version
    stale = await chats.manage("GET", params={"page_size": 1, "cursor": cursor})
    assert (
        stale.status_code == 409 and stale.json()["error"]["code"] == "policy_changed"
    )


@pytest.mark.asyncio
async def test_history_projects_safe_notices_and_withholds_text_and_stale_turns(chats):
    first = await chats.send("POST", chats.turn_path, chats.payload)
    assert first.status_code == 200, first.text
    later = {**chats.payload, "request_id": str(uuid4()), "expected_version": 1}
    assert (await chats.send("POST", chats.turn_path, later)).status_code == 200
    parent = f"{CHATS}/{chats.fixture.parent.id}/turns"
    response = await chats.manage("GET", parent, params={"page_size": 1})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (
        data["turns"][0]["sequence"] == 1 and data["turns"][0]["user_message"] is None
    )
    assert chats.payload["message"] not in response.text and data["next_cursor"]
    next_page = await chats.manage(
        "GET", parent, params={"page_size": 1, "cursor": data["next_cursor"]}
    )
    assert (
        next_page.status_code == 200
        and next_page.json()["data"]["turns"][0]["sequence"] == 2
    )
    version = chats.fixture.decision().policy_version + 1
    chats.fixture.saved.snapshot = chats.fixture.saved.snapshot.model_copy(
        update={"policy_version": version}
    )
    chats.fixture.database["app_settings"].documents["access_control"][
        "policy_version"
    ] = version
    hidden = await chats.manage("GET", parent)
    assert hidden.status_code == 200 and hidden.json()["data"]["turns"] == []
    assert hidden.json()["data"]["notice"] is not None
    chats.allowed = False
    assert (await chats.manage("GET", parent)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mark_chat_deleting", "delete_chat_turns"])
async def test_delete_reports_cleanup_pending_and_resumes_without_resurrection(
    chats, monkeypatch, failure
):
    assert (await chats.send("POST", chats.turn_path, chats.payload)).status_code == 200
    path = f"{CHATS}/{chats.fixture.parent.id}"

    async def fail(*_args, **_kwargs):
        raise AppDatabaseUnavailable("synthetic private driver reason")

    with monkeypatch.context() as m:
        m.setattr(_AppWriteTransaction, failure, fail)
        pending = await chats.manage("DELETE", path)
    assert (
        pending.status_code == 503
        and pending.json()["error"]["code"] == "cleanup_pending"
    )
    assert "private driver reason" not in pending.text
    if failure == "delete_chat_turns":
        assert (await chats.manage("GET", f"{path}/turns")).status_code == 404
    assert (await chats.manage("DELETE", path)).status_code == 204
    assert (await chats.manage("DELETE", path)).status_code == 204
    assert (await chats.manage("GET", f"{path}/turns")).status_code == 404
    assert not chats.fixture.turns.documents
    assert str(chats.fixture.parent.id) not in chats.fixture.sessions.documents
