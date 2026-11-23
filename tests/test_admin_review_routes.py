"""Signed administrator review with synthetic users and a transactional store."""

from copy import deepcopy
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.admin_review_routes import AdminReviewAPI
from goodshowroom_ai.app import create_app
from goodshowroom_ai.authorization import AuthorizationService
from goodshowroom_ai.config import Settings
from goodshowroom_ai.identity import operation_digest
from goodshowroom_ai.temporary import TemporaryMemoryStore
from tests.test_chat_history import (
    _admin_repository,
    _assert_content_free_review_audit,
    add_turn,
    history_fixture,
)
from tests.test_chat_turns import NOW, notice_response
from tests.test_identity_bridge import _mint, _request_claims, _verifier

REVIEWS = "/v1/admin/conversations"
PRIVATE_MESSAGE = "Synthetic private question and internal trace marker"


class ReviewRequests:
    async def configure(self, signing_key):
        self.key = signing_key
        self.fixture = history_fixture()
        self.protected = await add_turn(self.fixture, 1, message=PRIVATE_MESSAGE)
        self.path = f"{REVIEWS}/{self.fixture.parent.id}/turns"
        self.owner = self.fixture.saved
        self.user_id = str(self.owner.owner_id)
        self.reviewer, _, _, _ = _admin_repository(self.fixture)
        self.clock = NOW

        async def load_policy(principal):
            subjects = {
                self.reviewer.owner_id: self.reviewer,
                self.owner.owner_id: self.owner,
            }
            return subjects[principal.user_id].snapshot

        async def active(principal):
            snapshot = await load_policy(principal)
            return snapshot.login.id == principal.login_id and snapshot.login.valid(
                self.clock
            )

        authorization = AuthorizationService(
            load_policy=load_policy, check_identity=active, clock=lambda: self.clock
        )
        self.memory = TemporaryMemoryStore(
            authorization=authorization, clock=lambda: self.clock
        )
        self.app = create_app(
            Settings(),
            temporary_memory=self.memory,
            admin_review=AdminReviewAPI(
                self.owner.app_database,
                _verifier(signing_key),
                authorization,
                lambda: self.clock,
                self.owner.codec,
            ),
        )

    async def add_notice(self):
        command = self.fixture.command(expected_version=1, message=PRIVATE_MESSAGE)
        decision = self.fixture.decision()
        attempt = await self.fixture.repository.begin_turn_attempt(
            decision, self.fixture.parent.id, command
        )
        result = await self.fixture.repository.commit_completed_turn(
            decision,
            self.fixture.parent.id,
            command,
            notice_response(command, decision.policy_version),
            attempt.run_id,
        )
        return result.turn

    async def get(
        self,
        path=REVIEWS,
        *,
        subject=None,
        query=None,
        method="GET",
        tamper=False,
        body=b"",
        signed=True,
    ):
        subject = subject or self.reviewer
        query = urlencode(query if query is not None else {"user_id": self.user_id})
        stamp = int(self.clock.timestamp())
        token = _mint(
            self.key,
            _request_claims(
                sub=str(subject.owner_id),
                login_session_id=str(subject.login_id),
                generation=subject.login.generation,
                method=method,
                path=path,
                iat=stamp,
                nbf=stamp,
                exp=stamp + 90,
                digest=operation_digest(method, path, query.encode("ascii"), body),
            ),
        )
        actual_query = query + "&page_size=3" if tamper else query
        async with AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test.invalid"
        ) as client:
            return await client.request(
                method,
                f"{path}?{actual_query}",
                content=body,
                headers={"Authorization": f"Bearer {token}"} if signed else {},
            )

    def audits(self):
        return [
            event
            for event in self.fixture.audits.documents.values()
            if event["action"] == "admin_review"
        ]


@pytest_asyncio.fixture
async def review(signing_key):
    case = ReviewRequests()
    await case.configure(signing_key)
    try:
        yield case
    finally:
        await case.memory.close()


@pytest.mark.asyncio
async def test_review_is_read_only_audited_and_separate_from_temporary_memory(review):
    notice = await review.add_notice()
    temporary = await review.memory.create(review.owner.principal, uuid4())
    before = deepcopy(
        (review.fixture.sessions.documents, review.fixture.turns.documents)
    )

    chats = await review.get()
    assert chats.status_code == 200, chats.text
    assert chats.headers["cache-control"] == "no-store"
    listing = chats.json()["data"]
    assert set(listing) == {"chats", "next_cursor"}
    assert [chat["id"] for chat in listing["chats"]] == [str(review.fixture.parent.id)]
    assert set(listing["chats"][0]) == {"id", "title", "last_activity_at"}
    history = await review.get(review.path)
    assert history.status_code == 200, history.text
    data = history.json()["data"]
    assert data["notice"]
    assert [turn["id"] for turn in data["turns"]] == [str(notice.id)]
    assert set(data["turns"][0]) == {"id", "committed_at", "response"}
    assert data["turns"][0]["response"]["status"] == "unsupported"
    for value in (
        PRIVATE_MESSAGE,
        str(review.protected.id),
        str(temporary.id),
        str(review.owner.owner_id),
        str(review.reviewer.owner_id),
    ):
        assert value not in chats.text and value not in history.text

    for identifier in (temporary.id, uuid4()):
        denied = await review.get(f"{REVIEWS}/{identifier}/turns")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "access_denied"
    assert (await review.get(review.path, method="DELETE")).status_code == 405
    assert before == (review.fixture.sessions.documents, review.fixture.turns.documents)
    assert len(review.audits()) == 2
    for event in review.audits():
        _assert_content_free_review_audit(
            event, actor_id=review.reviewer.owner_id, target_id=review.owner.owner_id
        )


@pytest.mark.asyncio
async def test_review_refuses_unsigned_requests_and_users_without_review_capability(
    review,
):
    assert (await review.get(signed=False)).status_code == 401
    assert (await review.get(tamper=True)).status_code == 401
    assert (await review.get(body=b"{}")).status_code == 401
    for path in (REVIEWS, review.path):
        assert (await review.get(path, subject=review.owner)).status_code == 403
    assert not review.audits()

    await review.add_notice()
    assert (await review.get(review.path)).json()["data"]["turns"]
    settings = review.fixture.database["app_settings"].documents["access_control"]
    settings["policy_version"] += 1
    review.reviewer.snapshot = review.reviewer.snapshot.model_copy(
        update={"policy_version": settings["policy_version"]}
    )
    hidden = await review.get(review.path)
    assert hidden.status_code == 200 and hidden.json()["data"]["turns"] == []
    role = review.reviewer.role.model_copy(
        update={"capabilities": frozenset({"manage_access"})}
    )
    review.reviewer.snapshot = review.reviewer.snapshot.model_copy(
        update={"role": role}
    )
    review.fixture.database["roles"].documents[str(role.id)] = role.document()
    assert (await review.get(review.path)).status_code == 403
    assert len(review.audits()) == 2


@pytest.mark.asyncio
async def test_review_audit_failure_rolls_back_and_returns_no_data(review):
    settings = review.fixture.database["app_settings"].documents["access_control"]
    original_fence = settings["commit_fence"]
    review.fixture.audits.fail_insert = True
    for path in (REVIEWS, review.path):
        unavailable = await review.get(path)
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["error"]["code"] == "source_unavailable"
        assert "data" not in unavailable.json() and "simulated" not in unavailable.text
    assert not review.audits()
    assert (
        review.fixture.database["app_settings"].documents["access_control"][
            "commit_fence"
        ]
        == original_fence
    )


@pytest.mark.asyncio
async def test_filter_bounds_and_cursor_binding(review):
    await review.fixture.create_parent()
    start = NOW.date().isoformat()
    end = NOW.date().replace(year=NOW.year + 1).isoformat()
    query = {"user_id": review.user_id, "page_size": "1", "since": start, "until": end}
    first = await review.get(query=query)
    assert first.status_code == 200, first.text
    page = first.json()["data"]
    assert page["next_cursor"] and len(page["chats"]) == 1
    second = await review.get(query={**query, "cursor": page["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["data"]["chats"] != page["chats"]
    assert not second.json()["data"]["next_cursor"]
    for changed in (
        {"user_id": str(uuid4())},
        {"since": "2026-01-01", "until": "2026-01-02"},
    ):
        mismatch = await review.get(
            query={**query, "cursor": page["next_cursor"], **changed}
        )
        assert mismatch.status_code == 422
    assert (await review.get(review.path, query=query)).status_code == 422
    assert len(review.audits()) == 2


@pytest.mark.asyncio
async def test_invalid_filters_are_rejected_without_audit(review):
    filters = {"user_id": review.user_id}
    for invalid in (
        {**filters, "since": "2026-10-23"},
        {**filters, "since": "2026-10-23T00:00:00Z", "until": "2026-10-24"},
        {**filters, "since": "2026-10-23", "until": "2099-01-01"},
        {**filters, "page_size": "101"},
        {**filters, "email": "example@example.invalid"},
        [("user_id", review.user_id), ("user_id", str(uuid4()))],
        {},
    ):
        assert (await review.get(query=invalid)).status_code == 422
    assert not review.audits()
