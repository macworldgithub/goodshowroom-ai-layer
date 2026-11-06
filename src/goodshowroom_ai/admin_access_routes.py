"""Fixed administrator access-management HTTP operations."""

from uuid import UUID, uuid4
from urllib.parse import parse_qsl

from fastapi import Request

from goodshowroom_ai.api_contracts import (
    AdminDecisionRequest,
    AdminMutationEnvelope,
    AdminPreapprovalRequest,
    AdminRequestPageEnvelope,
    AdminRevokeRequest,
    ErrorCode,
    parse_contract,
)
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.own_access_routes import _response


def _token(request):
    headers = request.headers.getlist("authorization")
    if (
        len(headers) != 1
        or not headers[0].startswith("Bearer ")
        or len(headers[0]) > 8199
    ):
        raise IdentityDenied("Identity assertion denied.")
    token = headers[0][7:]
    if not token or " " in token:
        raise IdentityDenied("Identity assertion denied.")
    return token


async def _change(request: Request, model, target=None):
    token = _token(request)
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    body = await request.body()
    service = getattr(request.app.state, "admin_access", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="POST", path=path, body=body)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    if target is not None:
        try:
            parsed = UUID(target)
            if str(parsed) != target:
                raise ValueError()
            target = parsed
        except ValueError:
            raise ApiProblem(ErrorCode.INVALID_REQUEST) from None
    data = parse_contract(model, body)
    request_id = uuid4()
    result = await service.change(
        token,
        path=path,
        body=body,
        request_id=request_id,
        data=data,
        target=target,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminMutationEnvelope(request_id=request_id, data=result)
    return _response(
        content=envelope.model_dump(mode="json"),
        status_code=201 if model is AdminPreapprovalRequest else 200,
    )


async def decision(request: Request, request_id: str):
    return await _change(request, AdminDecisionRequest, request_id)


async def preapprove(request: Request):
    return await _change(request, AdminPreapprovalRequest)


async def revoke_preapproval(request: Request, request_id: str):
    return await _change(request, AdminRevokeRequest, request_id)


async def list_requests(request: Request):
    token = _token(request)
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    query = request.scope.get("query_string", b"")
    service = getattr(request.app.state, "admin_access", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="GET", path=path, query=query)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    try:
        pairs = parse_qsl(query.decode("ascii"), strict_parsing=True, max_num_fields=3)
        values = dict(pairs)
        if len(values) != len(pairs) or set(values) - {"status", "page_size", "cursor"}:
            raise ValueError()
        status = values.get("status", "pending")
        page_size = values.get("page_size", "50")
        cursor = values.get("cursor")
        if status not in {"pending", "approved", "rejected"} or (
            not page_size.isascii()
            or not page_size.isdigit()
            or str(int(page_size)) != page_size
            or not 1 <= int(page_size) <= 100
            or cursor is not None
            and not 1 <= len(cursor) <= 4096
        ):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None
    result = await service.list_requests(
        token,
        path=path,
        query=query,
        status=status,
        page_size=int(page_size),
        cursor=cursor,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminRequestPageEnvelope(request_id=uuid4(), data=result)
    return _response(content=envelope.model_dump(mode="json"))
