"""ASGI tests for request bytes checked before route parsing or execution."""

from fastapi import Request
from httpx import ASGITransport, AsyncClient
import pytest

from goodshowroom_ai.api_contracts import ErrorCode, MAX_REQUEST_BYTES
from goodshowroom_ai.app import create_app
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.config import Settings


def make_probe_app():
    app = create_app(Settings(_env_file=None, app_env="test"))
    app.state.parsed_bodies = 0

    @app.post("/probe")
    async def probe(request: Request):
        app.state.parsed_bodies += 1
        return {"length": len(await request.body())}

    return app


async def post_body(app, body):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as client:
        return await client.post("/probe", content=body)


@pytest.mark.asyncio
async def test_exact_request_byte_limit_reaches_fastapi():
    app = make_probe_app()
    body = b"x" * MAX_REQUEST_BYTES

    response = await post_body(app, body)

    assert response.status_code == 200
    assert response.json() == {"length": MAX_REQUEST_BYTES}
    assert app.state.parsed_bodies == 1


@pytest.mark.asyncio
async def test_request_one_byte_below_limit_reaches_fastapi():
    app = make_probe_app()
    body = b"x" * (MAX_REQUEST_BYTES - 1)

    response = await post_body(app, body)

    assert response.status_code == 200
    assert response.json() == {"length": MAX_REQUEST_BYTES - 1}
    assert app.state.parsed_bodies == 1


@pytest.mark.asyncio
async def test_request_over_byte_limit_is_rejected_before_route_parsing():
    app = make_probe_app()
    marker = b"private-input-marker"
    body = b"x" * MAX_REQUEST_BYTES + marker

    response = await post_body(app, body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert app.state.parsed_bodies == 0
    assert marker.decode() not in response.text


@pytest.mark.parametrize(
    ("path", "code", "retry_after"),
    [
        ("/limited", ErrorCode.RATE_LIMITED, "60"),
        ("/busy", ErrorCode.REQUEST_IN_PROGRESS, "1"),
    ],
)
@pytest.mark.asyncio
async def test_admission_errors_use_fixed_retry_after_and_safe_body(
    path, code, retry_after
):
    app = create_app(Settings(_env_file=None, app_env="test"))
    marker = "private-input-marker"

    @app.get("/limited")
    async def limited():
        try:
            raise RuntimeError(marker)
        except RuntimeError as error:
            raise ApiProblem(ErrorCode.RATE_LIMITED) from error

    @app.get("/busy")
    async def busy():
        try:
            raise RuntimeError(marker)
        except RuntimeError as error:
            raise ApiProblem(ErrorCode.REQUEST_IN_PROGRESS) from error

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test.invalid"
    ) as client:
        response = await client.get(path)

    assert response.headers["retry-after"] == retry_after
    assert response.json()["error"]["code"] == code.value
    assert marker not in response.text
