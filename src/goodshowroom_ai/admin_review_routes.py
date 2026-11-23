"""Signed, audited read routes for saved conversation review."""

from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import Request

from goodshowroom_ai.admin_access_routes import _token
from goodshowroom_ai.admin_management_routes import _parse_uuid_param
from goodshowroom_ai.api_contracts import (
    ErrorCode,
    ReviewChatEnvelope,
    ReviewChatSummary,
    ReviewTurn,
    ReviewTurnEnvelope,
)
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.authorization import ProtectedAction
from goodshowroom_ai.execution import DEFAULT_PAGE_SIZE
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.saved_chat_routes import (
    _page_values,
    _public,
    can_release_saved_notice,
)
from goodshowroom_ai.saved_turn_routes import SignedChatAccess
from goodshowroom_ai.storage.chat_repository import ChatRepository
from goodshowroom_ai.storage.mongo import AppDatabaseUnavailable, RequiredAuditMissing

_MAX_QUERY_BYTES = 4600
_MAX_DATE_WINDOW = timedelta(days=366)


class AdminReviewAPI(SignedChatAccess):
    def __init__(self, database, verifier, authorization, clock, codec):
        super().__init__(database, verifier, clock)
        self._authorization = authorization
        self._repository = ChatRepository(
            database,
            codec,
            clock=clock,
            authorization_recheck=authorization.recheck,
            turn_access_check=can_release_saved_notice,
        )

    async def authorize(self, principal, owner):
        return await self._authorization.authorize(
            principal,
            ProtectedAction(action="review_saved_chats", owner_id=owner, mode="saved"),
        )

    async def list_conversations(self, decision, page):
        result = await self._repository.review_saved_chats(
            decision, owner_user_id=decision.request.owner_id, **page
        )
        return ReviewChatEnvelope(
            request_id=uuid4(),
            data={
                "chats": tuple(
                    ReviewChatSummary(
                        id=chat.id,
                        title=chat.title,
                        last_activity_at=chat.last_activity_at,
                    )
                    for chat in result.chats
                ),
                "next_cursor": result.next_cursor,
            },
        )

    async def list_turns(self, decision, chat_id, page):
        result = await self._repository.review_saved_turns(decision, chat_id, **page)
        return ReviewTurnEnvelope(
            request_id=uuid4(),
            data={
                "turns": tuple(
                    ReviewTurn(
                        id=turn.id,
                        response=turn.response,
                        committed_at=turn.committed_at,
                    )
                    for turn in result.turns
                ),
                "next_cursor": result.next_cursor,
                "notice": result.notice,
            },
        )


def _day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError()
    return datetime.combine(parsed, time.min, tzinfo=UTC)


def _filters(query, *, date_filters):
    allowed = {"user_id", "page_size", "cursor"}
    if date_filters:
        allowed.update(("since", "until"))
    try:
        pairs = parse_qsl(
            query.decode("ascii"),
            strict_parsing=True,
            max_num_fields=len(allowed),
            errors="strict",
        )
        values = dict(pairs)
        if (
            len(values) != len(pairs)
            or set(values) - allowed
            or "user_id" not in values
        ):
            raise ValueError()
        page = _page_values(
            values.get("page_size", str(DEFAULT_PAGE_SIZE)), values.get("cursor")
        )
        if "since" in values or "until" in values:
            if "since" not in values or "until" not in values:
                raise ValueError()
            since, until = _day(values["since"]), _day(values["until"])
            if not timedelta() < until - since <= _MAX_DATE_WINDOW:
                raise ValueError()
            page.update(since=since, until=until)
    except ValueError:
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None
    return _parse_uuid_param(values["user_id"]), page


async def _review(request, chat_id=None):
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    query = request.scope.get("query_string", b"")
    if len(query) > _MAX_QUERY_BYTES:
        raise ApiProblem(ErrorCode.INVALID_REQUEST)
    token = _token(request)
    api = request.app.state.admin_review
    if api is None:
        raise ApiProblem(ErrorCode.SOURCE_UNAVAILABLE)
    try:
        principal = await api.principal(
            token, request.method, request.url.path, b"", query=query
        )
        await request.app.state.admission.admit_admin_request(principal.user_id)
        owner, page = _filters(query, date_filters=chat_id is None)
        decision = await api.authorize(principal, owner)
        if chat_id is None:
            result = await api.list_conversations(decision, page)
        else:
            result = await api.list_turns(decision, _parse_uuid_param(chat_id), page)
    except (AppDatabaseUnavailable, RequiredAuditMissing):
        raise ApiProblem(ErrorCode.SOURCE_UNAVAILABLE) from None
    return _public(result)


async def list_review_conversations(request: Request):
    return await _review(request)


async def list_review_turns(request: Request, chat_id: str):
    return await _review(request, chat_id)
