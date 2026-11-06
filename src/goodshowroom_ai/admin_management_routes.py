"""Fixed administrator role and user-access management HTTP operations."""

from urllib.parse import parse_qsl
from uuid import UUID, uuid4

from fastapi import Request

from goodshowroom_ai.admin_access_routes import _token
from goodshowroom_ai.api_contracts import (
    AdminRoleCreateRequest,
    AdminRoleMutationEnvelope,
    AdminRolePageEnvelope,
    AdminRoleUpdateRequest,
    AdminUserAccessMutationEnvelope,
    AdminUserAccessUpdateRequest,
    AdminUserPageEnvelope,
    ErrorCode,
    parse_contract,
)
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.own_access_routes import _response


def _parse_uuid_param(param: str) -> UUID:
    try:
        parsed = UUID(param)
        if str(parsed) != param:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None


async def list_admin_users(request: Request):
    token = _token(request)
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    query = request.scope.get("query_string", b"")
    service = getattr(request.app.state, "admin_management", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="GET", path=path, query=query)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    try:
        pairs = parse_qsl(query.decode("ascii"), strict_parsing=True, max_num_fields=3)
        values = dict(pairs)
        if len(values) != len(pairs) or set(values) - {
            "approval_status",
            "page_size",
            "cursor",
        }:
            raise ValueError()
        status = values.get("approval_status", "approved")
        page_size = values.get("page_size", "25")
        cursor = values.get("cursor")
        if status not in {"pending", "approved", "rejected", "revoked"} or (
            not page_size.isascii()
            or not page_size.isdigit()
            or str(int(page_size)) != page_size
            or not 1 <= int(page_size) <= 100
            or (cursor is not None and not 1 <= len(cursor) <= 4096)
        ):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None

    result = await service.list_users(
        token,
        path=path,
        query=query,
        status=status,
        page_size=int(page_size),
        cursor=cursor,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminUserPageEnvelope(request_id=uuid4(), data=result)
    return _response(content=envelope.model_dump(mode="json"))


async def list_admin_roles(request: Request):
    token = _token(request)
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    query = request.scope.get("query_string", b"")
    service = getattr(request.app.state, "admin_management", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="GET", path=path, query=query)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    try:
        pairs = parse_qsl(query.decode("ascii"), strict_parsing=True, max_num_fields=3)
        values = dict(pairs)
        if len(values) != len(pairs) or set(values) - {
            "active",
            "page_size",
            "cursor",
        }:
            raise ValueError()
        active_raw = values.get("active")
        active_bool = None
        if active_raw is not None:
            if active_raw == "true":
                active_bool = True
            elif active_raw == "false":
                active_bool = False
            else:
                raise ValueError()
        page_size = values.get("page_size", "25")
        cursor = values.get("cursor")
        if (
            not page_size.isascii()
            or not page_size.isdigit()
            or str(int(page_size)) != page_size
            or not 1 <= int(page_size) <= 100
            or (cursor is not None and not 1 <= len(cursor) <= 4096)
        ):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None

    result = await service.list_roles(
        token,
        path=path,
        query=query,
        active=active_bool,
        page_size=int(page_size),
        cursor=cursor,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminRolePageEnvelope(request_id=uuid4(), data=result)
    return _response(content=envelope.model_dump(mode="json"))


async def create_admin_role(request: Request):
    token = _token(request)
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    body = await request.body()
    service = getattr(request.app.state, "admin_management", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="POST", path=path, body=body)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    data = parse_contract(AdminRoleCreateRequest, body)
    request_id = uuid4()
    result = await service.create_role(
        token,
        path=path,
        body=body,
        request_id=request_id,
        data=data,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminRoleMutationEnvelope(request_id=request_id, data=result)
    return _response(content=envelope.model_dump(mode="json"), status_code=201)


async def update_admin_role(request: Request, role_id: str):
    token = _token(request)
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    parsed_role_id = _parse_uuid_param(role_id)
    body = await request.body()
    service = getattr(request.app.state, "admin_management", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="PATCH", path=path, body=body)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    data = parse_contract(AdminRoleUpdateRequest, body)
    request_id = uuid4()
    result = await service.update_role(
        token,
        path=path,
        body=body,
        request_id=request_id,
        role_id=parsed_role_id,
        data=data,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminRoleMutationEnvelope(request_id=request_id, data=result)
    return _response(content=envelope.model_dump(mode="json"), status_code=200)


async def update_admin_user_access(request: Request, user_id: str):
    token = _token(request)
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    parsed_user_id = _parse_uuid_param(user_id)
    body = await request.body()
    service = getattr(request.app.state, "admin_management", None)
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    path = request.url.path
    principal, _ = await service.authorize(token, method="PATCH", path=path, body=body)
    await request.app.state.admission.admit_admin_request(principal.user_id)
    data = parse_contract(AdminUserAccessUpdateRequest, body)
    request_id = uuid4()
    result = await service.change_user_access(
        token,
        path=path,
        body=body,
        request_id=request_id,
        user_id=parsed_user_id,
        data=data,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminUserAccessMutationEnvelope(request_id=request_id, data=result)
    return _response(content=envelope.model_dump(mode="json"), status_code=200)
