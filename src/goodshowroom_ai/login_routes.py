"""Server-only login exchange. The verifier, not headers alone, grants identity."""

from uuid import uuid4

from fastapi import Request
from starlette.responses import JSONResponse, Response

from goodshowroom_ai.api_contracts import ErrorCode
from goodshowroom_ai.api_http import ApiProblem, SAFE_HEADERS
from goodshowroom_ai.identity import IdentityDenied


async def _assertion(request: Request) -> str:
    headers = request.headers.getlist("authorization")
    if (
        len(headers) != 1
        or not headers[0].startswith("Bearer ")
        or len(headers[0]) > 8199
        or request.scope.get("query_string", b"")
        or await request.body()
    ):
        raise IdentityDenied("Identity assertion denied.")
    token = headers[0][7:]
    if not token or " " in token:
        raise IdentityDenied("Identity assertion denied.")
    return token


async def exchange(request: Request):
    """Accept only a signed bootstrap assertion and an empty request body."""
    token = await _assertion(request)
    service = getattr(request.app.state, "login_workflow", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    request_id = uuid4()
    user, login = await service.exchange_current(token, request_id=request_id)
    return JSONResponse(
        headers=SAFE_HEADERS,
        content={
            "schema_version": 1,
            "request_id": str(request_id),
            "data": {
                "user_id": str(user.id),
                "login_session_id": str(login.id),
                "generation": login.generation,
                "access_status": user.approval_status,
            },
        },
    )


async def _change(request: Request, *, revoke: bool):
    token = await _assertion(request)
    service = getattr(request.app.state, "login_lifecycle", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    request_id = uuid4()
    login = await service.change(token, revoke=revoke, request_id=request_id)
    if revoke:
        return Response(status_code=204, headers=SAFE_HEADERS)
    return JSONResponse(
        headers=SAFE_HEADERS,
        content={
            "schema_version": 1,
            "request_id": str(request_id),
            "data": {"lease_valid_until": login.lease_valid_until.isoformat()},
        },
    )


async def renew(request: Request):
    return await _change(request, revoke=False)


async def revoke(request: Request):
    return await _change(request, revoke=True)
