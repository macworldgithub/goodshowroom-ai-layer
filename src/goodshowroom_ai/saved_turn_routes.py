"""Verified saved-turn execution and read-only request recovery."""

import asyncio
from uuid import UUID

from fastapi import Request
from starlette.responses import Response

from goodshowroom_ai.admin_access_routes import _token
from goodshowroom_ai.admin_management_routes import _parse_uuid_param
from goodshowroom_ai.api_contracts import (
    ChatResultEnvelope,
    ChatTurnData,
    ErrorCode,
    SavedRequestEnvelope,
    TurnRequest,
    parse_contract,
)
from goodshowroom_ai.api_http import SAFE_HEADERS, ApiProblem, success_response
from goodshowroom_ai.authorization import Principal
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.own_access import OwnAccessService
from goodshowroom_ai.own_access_routes import _response
from goodshowroom_ai.responses import ResponseContext, ResponseInvalid
from goodshowroom_ai.storage.chat_turns import TurnCommand
from goodshowroom_ai.storage.models import utc_datetime
from goodshowroom_ai.storage.mongo import AppDatabaseUnavailable


class SignedChatAccess:
    def __init__(self, database, verifier, clock):
        self._database = database
        self._verifier = verifier
        self._clock = clock

    async def principal(self, token, method, path, body, *, query=b""):
        async with self._database.snapshot_transaction(
            OwnAccessService._context(token)
        ) as tx:
            try:
                claims = await self._verifier.verify(
                    token,
                    purpose="request",
                    method=method,
                    path=path,
                    body=body,
                    query=query,
                    load_login=tx.get_login,
                    now=utc_datetime(self._clock()),
                )
            except IdentityDenied:
                claims = None
        if claims is None:
            raise IdentityDenied("Identity assertion denied.")
        return Principal(
            user_id=UUID(claims["sub"]),
            login_id=UUID(claims["login_session_id"]),
            generation=claims["generation"],
        )


class SavedTurnAPI(SignedChatAccess):
    def __init__(self, database, verifier, agent, clock):
        super().__init__(database, verifier, clock)
        self._agent = agent

    async def execute(self, principal, context, command):
        try:
            outcome = await self._agent.run_turn(principal, context, command)
        except ApiProblem as error:
            if error.code == ErrorCode.REQUEST_IN_PROGRESS:
                state, recovered = await self._agent.recover_saved_request(
                    principal, context, command
                )
                if state == "unknown":
                    raise ApiProblem(
                        ErrorCode.PERSISTENCE_FAILED, commit_state="unknown"
                    ) from None
                if recovered is not None:
                    return _committed(context, recovered)
            if error.code == ErrorCode.DEADLINE_EXCEEDED:
                raise ApiProblem(error.code, commit_state="unknown") from None
            raise
        except AppDatabaseUnavailable:
            raise ApiProblem(
                ErrorCode.PERSISTENCE_FAILED, commit_state="unknown"
            ) from None
        if outcome.status == "unknown":
            raise ApiProblem(ErrorCode.PERSISTENCE_FAILED, commit_state="unknown")
        if outcome.status == "processing":
            raise ApiProblem(ErrorCode.REQUEST_IN_PROGRESS)
        return _committed(context, outcome)

    async def status(self, principal, context):
        status, outcome = await self._agent.recover_saved_request(principal, context)
        if status is None:
            raise ApiProblem(ErrorCode.NOT_FOUND)
        return SavedRequestEnvelope(
            request_id=context.request_id,
            data={
                "status": status,
                "result": _committed(context, outcome) if outcome is not None else None,
            },
        )


def _committed(context, outcome):
    turn, built = outcome.turn, outcome.built
    if (
        outcome.status != "committed"
        or turn is None
        or built is None
        or turn.owner_user_id != context.owner_id
        or turn.session_id != context.session_id
        or turn.request_id != context.request_id
        or built.request_id != context.request_id
        or turn.response != built.response
    ):
        raise ResponseInvalid("Response is invalid.")
    return ChatTurnData(
        session_id=context.session_id,
        mode="saved",
        version=turn.sequence,
        persistence="committed",
        response=built.response,
    )


async def _authenticated(request, body):
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    token = _token(request)
    api = request.app.state.saved_turns
    if api is None:
        raise ApiProblem(ErrorCode.SOURCE_UNAVAILABLE)
    return api, await api.principal(token, request.method, request.url.path, body)


def _context(principal, session_id, request_id):
    return ResponseContext(
        request_id=request_id,
        session_id=session_id,
        owner_id=principal.user_id,
        mode="saved",
    )


async def _execute_until_disconnect(request, work):
    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    execution = asyncio.create_task(work)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait(
            (execution, watcher), return_when=asyncio.FIRST_COMPLETED
        )
        if watcher in done:
            watcher.result()
            return None
        return execution.result()
    finally:
        for task in (execution, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(execution, watcher, return_exceptions=True)


async def post_saved_turn(request: Request, chat_id: str):
    body = await request.body()
    api, principal = await _authenticated(request, body)
    session_id = _parse_uuid_param(chat_id)
    data = parse_contract(TurnRequest, body)
    request.state.contract_request_id = data.request_id
    context = _context(principal, session_id, data.request_id)
    result = await _execute_until_disconnect(
        request, api.execute(principal, context, TurnCommand(**data.model_dump()))
    )
    if result is None:
        return Response(status_code=499, headers=SAFE_HEADERS)
    return success_response(ChatResultEnvelope(request_id=data.request_id, data=result))


async def get_saved_request(request: Request, chat_id: str, request_id: str):
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    api, principal = await _authenticated(request, b"")
    context = _context(
        principal, _parse_uuid_param(chat_id), _parse_uuid_param(request_id)
    )
    request.state.contract_request_id = context.request_id
    await request.app.state.admission.admit_saved_status(principal.user_id)
    envelope = await api.status(principal, context)
    checked = parse_contract(
        SavedRequestEnvelope, envelope.model_dump_json(warnings="error").encode()
    )
    return _response(content=checked.model_dump(mode="json"))
