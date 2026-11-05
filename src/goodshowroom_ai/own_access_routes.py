"""Fixed server-authenticated own-access HTTP routes."""

from uuid import uuid4

from fastapi import Request
from starlette.responses import JSONResponse

from goodshowroom_ai.api_contracts import (
    ErrorCode,
    OwnAccessData,
    OwnAccessEnvelope,
    OwnRequestData,
    OwnRequestEnvelope,
)
from goodshowroom_ai.api_http import ApiProblem, SAFE_HEADERS
from goodshowroom_ai.login_routes import _assertion
from goodshowroom_ai.execution import MAX_WIRE_BYTES
from goodshowroom_ai.responses import ResponseInvalid


def _response(content, *, status_code=200):
    response = JSONResponse(
        status_code=status_code, headers=SAFE_HEADERS, content=content
    )
    if len(response.body) > MAX_WIRE_BYTES:
        raise ResponseInvalid("Response is invalid.")
    return response


async def me(request: Request):
    token = await _assertion(request)
    service = getattr(request.app.state, "own_access", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    data = OwnAccessData.model_validate(await service.status(token))
    envelope = OwnAccessEnvelope(request_id=uuid4(), data=data)
    return _response(
        content=envelope.model_dump(mode="json"),
    )


async def create_request(request: Request):
    token = await _assertion(request)
    service = getattr(request.app.state, "own_access", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    request_id = uuid4()
    result, created = await service.request(token, request_id)
    envelope = OwnRequestEnvelope(
        request_id=request_id, data=OwnRequestData(id=result.id, status=result.status)
    )
    return _response(
        status_code=201 if created else 200,
        content=envelope.model_dump(mode="json"),
    )
