"""Fixed admin selectors, identity mapping, and settings routes."""

from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import Request

from goodshowroom_ai.admin_access_routes import _token
from goodshowroom_ai.admin_management_routes import _parse_uuid_param
from goodshowroom_ai.admin_staff import safe_staff_name
from goodshowroom_ai.api_contracts import (
    AdminMappingEnvelope,
    AdminMappingRequest,
    AdminOptionsEnvelope,
    AdminSettingsEnvelope,
    AdminSettingsRequest,
    ErrorCode,
    parse_contract,
)
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.own_access_routes import _response


async def _authorize(request, *, method, action, query=b"", body=b""):
    token = _token(request)
    service = request.app.state.admin_configuration
    if service is None:
        raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
    principal, decision = await service.authorize(
        token,
        method=method,
        action=action,
        path=request.url.path,
        query=query,
        body=body,
    )
    await request.app.state.admission.admit_admin_request(principal.user_id)
    return service, principal, decision


async def access_options(request: Request):
    if await request.body():
        raise IdentityDenied("Identity assertion denied.")
    query = request.scope.get("query_string", b"")
    service, principal, decision = await _authorize(
        request, method="GET", action="manage_access", query=query
    )
    try:
        pairs = parse_qsl(
            query.decode("ascii"),
            strict_parsing=True,
            max_num_fields=3,
            errors="strict",
        )
        values = dict(pairs)
        dataset, site, name = (
            values.get(key) for key in ("dataset", "site", "staff_name")
        )
        if (
            len(pairs) != len(values)
            or set(values) - {"dataset", "site", "staff_name"}
            or dataset not in {None, "byd-leads-new", "byd-panel"}
            or site is not None
            and (
                not 1 <= len(site) <= 100
                or not site.isascii()
                or any(not (c.isalnum() or c in "_-") for c in site)
            )
            or name is not None
            and (site is None or dataset is None or not safe_staff_name(name))
        ):
            raise ValueError()
    except ValueError:
        raise ApiProblem(ErrorCode.INVALID_REQUEST) from None
    data = await service.options(
        principal,
        decision,
        dataset=dataset,
        site=site,
        staff_name=name,
        codec=request.app.state.cursor_codec,
    )
    envelope = AdminOptionsEnvelope(request_id=uuid4(), data=data)
    return _response(content=envelope.model_dump(mode="json"))


async def put_identity_mapping(request: Request, user_id: str, dataset_id: str):
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    body = await request.body()
    service, principal, decision = await _authorize(
        request, method="PUT", action="manage_access", body=body
    )
    if dataset_id not in {"byd-leads-new", "byd-panel"}:
        raise ApiProblem(ErrorCode.INVALID_REQUEST)
    data = parse_contract(AdminMappingRequest, body)
    request_id = uuid4()
    result = await service.map_staff(
        principal,
        decision,
        _parse_uuid_param(user_id),
        dataset_id,
        data,
        request.app.state.cursor_codec,
        request_id,
    )
    envelope = AdminMappingEnvelope(request_id=request_id, data=result)
    return _response(content=envelope.model_dump(mode="json"))


async def get_admin_settings(request: Request):
    if await request.body() or request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    service, principal, decision = await _authorize(
        request, method="GET", action="manage_settings"
    )
    data = await service.settings(principal, decision)
    envelope = AdminSettingsEnvelope(request_id=uuid4(), data=data)
    return _response(content=envelope.model_dump(mode="json"))


async def patch_admin_settings(request: Request):
    if request.scope.get("query_string", b""):
        raise IdentityDenied("Identity assertion denied.")
    body = await request.body()
    service, principal, decision = await _authorize(
        request, method="PATCH", action="manage_settings", body=body
    )
    data = parse_contract(AdminSettingsRequest, body)
    request_id = uuid4()
    result = await service.change_setting(principal, decision, data, request_id)
    envelope = AdminSettingsEnvelope(request_id=request_id, data=result)
    return _response(content=envelope.model_dump(mode="json"))
