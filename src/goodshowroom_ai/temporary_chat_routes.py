"""Signed, memory-only temporary chat routes. No durable fallback exists.

Every route verifies the signed login, applies a per-user limit, and then uses
the single-process temporary store. Nothing here reads or writes saved chats.
There is no list, history or review route for temporary chats.
"""

import asyncio
from uuid import UUID

from fastapi import Request
from starlette.responses import Response

from goodshowroom_ai.admin_access_routes import _token
from goodshowroom_ai.admin_management_routes import _parse_uuid_param
from goodshowroom_ai.api_contracts import (
    ChatResultEnvelope,
    ChatTurnData,
    CreateChatRequest,
    ErrorCode,
    TemporaryChatEnvelope,
    TemporaryRequestEnvelope,
    TurnRequest,
    parse_contract,
)
from goodshowroom_ai.api_http import SAFE_HEADERS, ApiProblem, success_response
from goodshowroom_ai.authorization import AccessDenied, Principal
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.responses import ResponseContext, ResponseInvalid
from goodshowroom_ai.saved_chat_routes import _public
from goodshowroom_ai.saved_turn_routes import (
    SignedChatAccess,
    _execute_until_disconnect,
)
from goodshowroom_ai.storage.chat_turns import TurnCommand
from goodshowroom_ai.storage.mongo import AuthorizationDenied
from goodshowroom_ai.temporary import TemporaryMemoryStore, TemporaryUnavailable


class TemporaryChatAPI(SignedChatAccess):
    def __init__(self, database, verifier, agent, memory, clock):
        super().__init__(database, verifier, clock)
        self._agent, self._memory = agent, memory

    async def create(self, principal, request_id):
        chat, created = await self._memory.create_with_status(principal, request_id)
        envelope = TemporaryChatEnvelope(
            request_id=request_id, data={"id": chat.id, "version": chat.version}
        )
        return envelope, created

    async def execute(self, principal, context, command):
        try:
            outcome = await self._agent.run_turn(principal, context, command)
        except (AccessDenied, AuthorizationDenied):
            # The agent checks access before the store does. Match the store's own
            # rule: a denial ends this login's temporary contexts.
            self._memory.discard_denied(principal)
            raise
        if outcome.status != "complete" or outcome.built is None:
            raise ResponseInvalid("Response is invalid.")
        return ChatTurnData(
            session_id=context.session_id,
            mode="temporary",
            version=command.expected_version + 1,
            persistence="not_saved",
            response=outcome.built.response,
        )

    async def status(self, principal, context):
        status, built, version = await self._agent.recover_temporary_request(
            principal, context
        )
        if status is None:
            raise ApiProblem(ErrorCode.NOT_FOUND)
        result = None
        if built is not None:
            result = ChatTurnData(
                session_id=context.session_id,
                mode="temporary",
                version=version,
                persistence="not_saved",
                response=built.response,
            )
        return TemporaryRequestEnvelope(
            request_id=context.request_id, data={"status": status, "result": result}
        )

    async def end(self, principal, chat_id: UUID):
        await self._memory.end(principal, chat_id)


def build_temporary_chat_api(database, verifier, clock, *, agent, memory):
    if not isinstance(memory, TemporaryMemoryStore):
        raise ValueError("A temporary memory store is required")
    return TemporaryChatAPI(database, verifier, agent, memory, clock)


def _context(principal: Principal, session_id: UUID, request_id: UUID):
    return ResponseContext(
        request_id=request_id,
        session_id=session_id,
        owner_id=principal.user_id,
        mode="temporary",
        chat_login_id=principal.login_id,
    )


async def _authenticated(request: Request, body: bytes, *, limited: bool = True):
    """Verify the signed login, then apply the per-user limit."""
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    token = _token(request)
    api = getattr(request.app.state, "temporary_chats", None)
    if api is None:
        raise TemporaryUnavailable()
    principal = await api.principal(token, request.method, request.url.path, body)
    if limited:
        await request.app.state.admission.admit_temporary_request(principal.user_id)
    return api, principal


async def create_temporary_chat(request: Request):
    body = await request.body()
    api, principal = await _authenticated(request, body)
    data = parse_contract(CreateChatRequest, body)
    request.state.contract_request_id = data.request_id
    envelope, created = await api.create(principal, data.request_id)
    return _public(envelope, 201 if created else 200)


async def post_temporary_turn(request: Request, chat_id: str):
    body = await request.body()
    # Turns use the admission controller inside the agent, not the shared window.
    api, principal = await _authenticated(request, body, limited=False)
    session_id = _parse_uuid_param(chat_id)
    data = parse_contract(TurnRequest, body)
    request.state.contract_request_id = data.request_id
    context = _context(principal, session_id, data.request_id)
    try:
        result = await _execute_until_disconnect(
            request, api.execute(principal, context, TurnCommand(**data.model_dump()))
        )
    except asyncio.CancelledError:
        # The chat ended, expired or logged out while this turn ran. A genuine
        # cancel of this request must still propagate.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        raise TemporaryUnavailable() from None
    if result is None:
        return Response(status_code=499, headers=SAFE_HEADERS)
    return success_response(ChatResultEnvelope(request_id=data.request_id, data=result))


async def get_temporary_request(request: Request, chat_id: str, request_id: str):
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    api, principal = await _authenticated(request, b"")
    context = _context(
        principal, _parse_uuid_param(chat_id), _parse_uuid_param(request_id)
    )
    request.state.contract_request_id = context.request_id
    return _public(await api.status(principal, context))


async def end_temporary_chat(request: Request, chat_id: str):
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    api, principal = await _authenticated(request, b"")
    session_id = _parse_uuid_param(chat_id)
    await api.end(principal, session_id)
    return Response(status_code=204, headers=SAFE_HEADERS)
