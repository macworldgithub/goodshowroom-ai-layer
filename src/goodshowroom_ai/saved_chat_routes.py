"""Owner-scoped saved-chat management and safe history projection."""

from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import Request
from starlette.responses import Response

from goodshowroom_ai.admin_access_routes import _token
from goodshowroom_ai.admin_management_routes import _parse_uuid_param
from goodshowroom_ai.api_contracts import (
    CreateChatRequest,
    ErrorCode,
    SavedChatData,
    SavedChatEnvelope,
    SavedChatPageEnvelope,
    SavedHistoryEnvelope,
    SavedHistoryItem,
    parse_contract,
)
from goodshowroom_ai.api_http import SAFE_HEADERS, ApiProblem
from goodshowroom_ai.authorization import ProtectedAction
from goodshowroom_ai.execution import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.own_access_routes import _response
from goodshowroom_ai.saved_turn_routes import SignedChatAccess
from goodshowroom_ai.storage.chat_repository import ChatRepository, SavedChatUnavailable


class SavedChatAPI(SignedChatAccess):
    def __init__(self, database, verifier, authorization, clock, repository):
        super().__init__(database, verifier, clock)
        self._authorization = authorization
        self._repository = repository

    async def use(self, principal, action):
        return await self._authorization.authorize(
            principal,
            ProtectedAction(
                action=action,
                owner_id=principal.user_id,
                mode="saved",
            ),
        )

    async def create(self, decision, request_id):
        record, created = await self._repository.create_saved_chat_with_status(
            decision,
            request_id,
        )
        await self._authorization.recheck(decision)
        return SavedChatEnvelope(
            request_id=request_id,
            data=SavedChatData(
                id=record.id,
                title=record.title,
                version=record.version,
                last_activity_at=record.last_activity_at,
            ),
        ), created

    async def list_chats(self, decision, page):
        result = await self._repository.list_saved_chats(decision, **page)
        await self._authorization.recheck(decision)
        return SavedChatPageEnvelope(
            request_id=uuid4(),
            data=result.model_dump(),
        )

    async def history(self, decision, session_id, page):
        result = await self._repository.list_saved_turns(decision, session_id, **page)
        turns = tuple(
            SavedHistoryItem(
                id=turn.id,
                sequence=turn.sequence,
                response=turn.response,
                committed_at=turn.committed_at,
            )
            for turn in result.turns
        )
        return SavedHistoryEnvelope(
            request_id=uuid4(),
            data={
                "turns": turns,
                "next_cursor": result.next_cursor,
                "notice": result.notice,
            },
        )

    async def delete(self, decision, session_id):
        await self._repository.delete_saved_chat(decision, session_id)


def build_saved_chat_api(database, verifier, authorization, clock, codec):
    async def can_release_notice(decision, turn):
        # Release only proven fixed, content-free notices at this configuration.
        # Raw historical messages are independently excluded from the HTTP DTO.
        return (
            turn.policy_version == decision.policy_version
            and turn.response.status != "complete"
        )

    repository = ChatRepository(
        database,
        codec,
        clock=clock,
        authorization_recheck=authorization.recheck,
        turn_access_check=can_release_notice,
    )
    return SavedChatAPI(database, verifier, authorization, clock, repository)


async def _authorized(request, action, *, allow_body=False, paged=False):
    body = await request.body()
    query = request.scope.get("query_string", b"")
    if (body and not allow_body) or (query and not paged):
        raise IdentityDenied("Identity assertion denied.")
    token = _token(request)
    api = request.app.state.saved_chats
    if api is None:
        raise ApiProblem(ErrorCode.SOURCE_UNAVAILABLE)
    principal = await api.principal(
        token, request.method, request.url.path, body, query=query
    )
    await request.app.state.admission.admit_saved_chat(principal.user_id)
    return api, await api.use(principal, action), body, query


def _page(query):
    try:
        pairs = parse_qsl(
            query.decode("ascii"),
            strict_parsing=True,
            max_num_fields=2,
            errors="strict",
        )
        values = dict(pairs)
        size = values.get("page_size", str(DEFAULT_PAGE_SIZE))
        cursor = values.get("cursor")
        if (
            len(values) != len(pairs)
            or set(values) - {"page_size", "cursor"}
            or not 1 <= len(size) <= len(str(MAX_PAGE_SIZE))
            or not size.isascii()
            or not size.isdigit()
            or str(int(size)) != size
            or not 1 <= int(size) <= MAX_PAGE_SIZE
            or cursor is not None
            and not 1 <= len(cursor) <= 4096
        ):
            raise ValueError()
    except ValueError:
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None
    return {"page_size": int(size), "cursor": cursor}


def _public(envelope, status=200):
    checked = parse_contract(
        type(envelope), envelope.model_dump_json(warnings="error").encode()
    )
    return _response(content=checked.model_dump(mode="json"), status_code=status)


async def create_saved_chat(request: Request):
    api, decision, body, _ = await _authorized(request, "chat", allow_body=True)
    data = parse_contract(CreateChatRequest, body)
    request.state.contract_request_id = data.request_id
    try:
        record, created = await api.create(decision, data.request_id)
    except SavedChatUnavailable:
        raise ApiProblem(ErrorCode.NOT_FOUND) from None
    return _public(record, 201 if created else 200)


async def list_saved_chats(request: Request):
    api, decision, _, query = await _authorized(request, "history", paged=True)
    return _public(await api.list_chats(decision, _page(query)))


async def list_saved_history(request: Request, chat_id: str):
    api, decision, _, query = await _authorized(request, "history", paged=True)
    try:
        envelope = await api.history(decision, _parse_uuid_param(chat_id), _page(query))
    except SavedChatUnavailable:
        raise ApiProblem(ErrorCode.NOT_FOUND) from None
    return _public(envelope)


async def delete_saved_chat(request: Request, chat_id: str):
    api, decision, _, _ = await _authorized(request, "delete_chat")
    await api.delete(decision, _parse_uuid_param(chat_id))
    return Response(status_code=204, headers=SAFE_HEADERS)
