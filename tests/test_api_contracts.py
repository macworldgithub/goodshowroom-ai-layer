"""TICKET-030 public wire contracts and strict JSON parsing."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import traceback
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from goodshowroom_ai.api_contracts import (
    COMMIT_ERROR_CODES,
    ERROR_CATALOG,
    MAX_REQUEST_BYTES,
    MAX_SAFE_INTEGER,
    ApiError,
    ChatResultEnvelope,
    ChatTurnData,
    CreateChatRequest,
    ErrorCode,
    ErrorEnvelope,
    InvalidContract,
    PUBLIC_MODELS,
    TurnRequest,
    parse_contract,
)
from goodshowroom_ai.api_http import ApiProblem, install_error_handlers
from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.export_contracts import OPENAPI_PATH, export_text, public_openapi
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.operational import SourceUnavailable
from goodshowroom_ai.responses import Coverage, StructuredResponse
from goodshowroom_ai.storage.mongo import SavedChatCleanupPending
from tests.test_responses import complete_response

REQUEST_ID = UUID("7ed11ca9-26b2-44d3-96f4-351258801ed1")
SESSION_ID = UUID("4e748442-544d-4fef-9679-07b0ad62999c")
FIXTURES_PATH = Path(__file__).resolve().parents[1] / "contracts" / "fixtures.json"


def request_payload(**changes):
    payload = {
        "request_id": str(REQUEST_ID),
        "message": "Show total leads.",
        "expected_version": 0,
    }
    payload.update(changes)
    return payload


def result_envelope(*, mode="saved", persistence="committed", response=None):
    return ChatResultEnvelope(
        request_id=REQUEST_ID,
        data=ChatTurnData(
            session_id=SESSION_ID,
            mode=mode,
            version=1,
            persistence=persistence,
            response=response or complete_response(),
        ),
    )


def notice_response(status):
    notices = {
        "partial": (
            "The available data is incomplete.",
            "incomplete_data",
            "partial",
        ),
        "clarification": (
            "Which permitted dealership should I use?",
            "scope_required",
            "unknown",
        ),
        "unsupported": (
            "This question is not supported yet.",
            "question_not_supported",
            "unknown",
        ),
        "unavailable": (
            "The required data is unavailable.",
            "missing_data",
            "unknown",
        ),
    }
    answer, warning, coverage = notices[status]
    return StructuredResponse(
        status=status,
        answer=answer,
        warnings=(warning,),
        coverage=Coverage(status=coverage),
        policy_version=1,
    )


def test_public_requests_parse_as_typed_models_at_field_limits():
    create = parse_contract(
        CreateChatRequest,
        json.dumps({"request_id": str(REQUEST_ID)}).encode(),
    )
    assert create == CreateChatRequest(request_id=REQUEST_ID)

    turn = parse_contract(
        TurnRequest,
        json.dumps(
            request_payload(message="x" * 4000, expected_version=MAX_SAFE_INTEGER)
        ).encode(),
    )
    assert turn.request_id == REQUEST_ID
    assert len(turn.message) == 4000
    assert turn.expected_version == MAX_SAFE_INTEGER


@pytest.mark.parametrize(
    "payload",
    [
        request_payload(message=""),
        request_payload(message="   "),
        request_payload(message="x" * 4001),
        request_payload(message=7),
        request_payload(expected_version=-1),
        request_payload(expected_version=True),
        request_payload(expected_version=MAX_SAFE_INTEGER + 1),
        request_payload(request_id="not-a-uuid"),
        request_payload(request_id="{7ed11ca9-26b2-44d3-96f4-351258801ed1}"),
    ],
    ids=[
        "empty-message",
        "blank-message",
        "message-too-long",
        "wrong-message-type",
        "negative-version",
        "boolean-version",
        "unsafe-integer-version",
        "malformed-uuid",
        "noncanonical-uuid",
    ],
)
def test_turn_requests_reject_invalid_fields_without_reflecting_values(payload):
    marker = "private-input-marker"
    body = json.dumps(payload).encode()

    with pytest.raises(InvalidContract) as error:
        parse_contract(TurnRequest, body)

    assert str(error.value) == "Request or response is invalid."
    assert marker not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
        b'"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1"}',
        (
            b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
            b'"message":"private-input-marker","expected_version":0,'
            b'"owner_id":"private-input-marker"}'
        ),
        (
            b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
            b'"message":"ok","expected_version":0,"extra":NaN}'
        ),
        (
            b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
            b'"message":"ok","expected_version":0,"extra":Infinity}'
        ),
        (
            b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
            b'"message":"ok","expected_version":0,"extra":1e999}'
        ),
        (
            b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
            b'"message":"ok","expected_version":0.0}'
        ),
        (
            b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",'
            b'"message":"ok","expected_version":0e0}'
        ),
        b'{"request_id":"7ed11ca9-26b2-44d3-96f4-351258801ed1",',
        b"\xff",
    ],
    ids=[
        "duplicate-key",
        "unknown-field",
        "nan",
        "infinity",
        "overflow-to-infinity",
        "float-number",
        "exponent-number",
        "malformed-json",
        "invalid-utf8",
    ],
)
def test_strict_json_parser_rejects_ambiguous_or_unsafe_json(raw):
    with pytest.raises(InvalidContract) as error:
        parse_contract(TurnRequest, raw)

    assert str(error.value) == "Request or response is invalid."
    assert "private-input-marker" not in str(error.value)


def test_strict_json_parser_rejects_excessive_nesting_and_body_size():
    nested = "0"
    for _ in range(21):
        nested = f'{{"x":{nested}}}'
    deep_body = (
        f'{{"request_id":"{REQUEST_ID}","message":"ok",'
        f'"expected_version":0,"extra":{nested}}}'
    ).encode()
    oversized_body = (
        f'{{"request_id":"{REQUEST_ID}","message":"ok",'
        f'"expected_version":0,"extra":"{"x" * MAX_REQUEST_BYTES}"}}'
    ).encode()
    assert len(oversized_body) > MAX_REQUEST_BYTES

    for body in (deep_body, oversized_body):
        with pytest.raises(
            InvalidContract, match="^Request or response is invalid\\.$"
        ):
            parse_contract(TurnRequest, body)


def test_parser_only_accepts_exported_public_models():
    with pytest.raises(InvalidContract, match="^Request or response is invalid\\.$"):
        parse_contract(StructuredResponse, b"{}")


def test_parser_requires_wire_fields_even_when_the_model_has_defaults():
    payload = json.loads(result_envelope().model_dump_json())
    payload.pop("schema_version")
    with pytest.raises(InvalidContract, match="^Request or response is invalid\\.$"):
        parse_contract(ChatResultEnvelope, json.dumps(payload).encode())

    payload = json.loads(result_envelope().model_dump_json())
    payload["data"]["response"].pop("warnings")
    with pytest.raises(InvalidContract, match="^Request or response is invalid\\.$"):
        parse_contract(ChatResultEnvelope, json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("mode", "persistence"),
    [("saved", "not_saved"), ("temporary", "committed")],
)
def test_chat_turn_persistence_must_match_mode(mode, persistence):
    with pytest.raises(ValidationError):
        ChatTurnData(
            session_id=SESSION_ID,
            mode=mode,
            version=1,
            persistence=persistence,
            response=complete_response(),
        )


def test_chat_turn_version_starts_at_one_and_requires_a_safe_integer():
    for version in (0, -1, True, MAX_SAFE_INTEGER + 1):
        with pytest.raises(ValidationError):
            ChatTurnData(
                session_id=SESSION_ID,
                mode="saved",
                version=version,
                persistence="committed",
                response=complete_response(),
            )


def test_zero_result_round_trips_with_uuid_and_utc_timestamps():
    envelope = result_envelope(response=complete_response(value=0))
    raw = envelope.model_dump_json().encode()
    parsed = parse_contract(ChatResultEnvelope, raw)
    payload = json.loads(parsed.model_dump_json())
    retrieved_at = payload["data"]["response"]["retrieved_at"]

    assert parsed.request_id == REQUEST_ID
    assert parsed.data.session_id == SESSION_ID
    assert parsed.data.response.status == "complete"
    assert parsed.data.response.facts[0].value == 0
    assert parsed.data.response.datasets[0].rows[0].value == 0
    assert parsed.data.response.retrieved_at.utcoffset() == timedelta(0)
    parsed_retrieved_at = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    assert parsed_retrieved_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "status", ["partial", "clarification", "unsupported", "unavailable"]
)
def test_noncomplete_success_states_round_trip_without_business_evidence(status):
    response = notice_response(status)
    envelope = result_envelope(
        mode="temporary", persistence="not_saved", response=response
    )

    parsed = parse_contract(ChatResultEnvelope, envelope.model_dump_json().encode())

    assert parsed.data.mode == "temporary"
    assert parsed.data.persistence == "not_saved"
    assert parsed.data.response.status == status
    assert not parsed.data.response.facts
    assert not parsed.data.response.datasets


@pytest.mark.parametrize("code", list(ERROR_CATALOG))
def test_error_catalog_entries_are_fixed_and_serializable(code):
    _status, message, retryable = ERROR_CATALOG[code]
    error = ApiError(code=code, message=message, retryable=retryable)
    envelope = ErrorEnvelope(request_id=REQUEST_ID, error=error)

    parsed = parse_contract(ErrorEnvelope, envelope.model_dump_json().encode())

    assert parsed.request_id == REQUEST_ID
    assert parsed.error.code == code
    assert parsed.error.message == message
    assert parsed.error.retryable is retryable
    assert parsed.error.commit_state is None


@pytest.mark.parametrize("code", list(COMMIT_ERROR_CODES))
@pytest.mark.parametrize("commit_state", ["not_committed", "unknown"])
def test_commit_errors_allow_only_explicit_uncertain_states(code, commit_state):
    _status, message, retryable = ERROR_CATALOG[code]
    error = ApiError(
        code=code,
        message=message,
        retryable=retryable,
        commit_state=commit_state,
    )
    assert error.commit_state == commit_state


def test_noncommit_errors_reject_commit_state_and_catalog_text_changes():
    _status, message, retryable = ERROR_CATALOG[ErrorCode.ACCESS_DENIED]
    with pytest.raises(ValidationError):
        ApiError(
            code=ErrorCode.ACCESS_DENIED,
            message=message,
            retryable=retryable,
            commit_state="unknown",
        )

    with pytest.raises(ValidationError):
        ApiError(
            code=ErrorCode.ACCESS_DENIED,
            message="private-input-marker",
            retryable=retryable,
        )

    with pytest.raises(ValidationError):
        ApiError(
            code=ErrorCode.ACCESS_DENIED,
            message=message,
            retryable=not retryable,
        )


def test_source_failure_uses_error_envelope_instead_of_unavailable_success():
    code = ErrorCode.SOURCE_UNAVAILABLE
    _status, message, retryable = ERROR_CATALOG[code]
    envelope = ErrorEnvelope(
        request_id=REQUEST_ID,
        error=ApiError(code=code, message=message, retryable=retryable),
    )

    parsed = parse_contract(ErrorEnvelope, envelope.model_dump_json().encode())

    assert parsed.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert not hasattr(parsed, "data")


def shared_fixture_cases():
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize(
    "fixture_name",
    ["schema_version_boolean_true", "counts_disclosed_integer_zero"],
)
def test_json_boolean_and_integer_literals_keep_distinct_wire_types(fixture_name):
    case = next(item for item in shared_fixture_cases() if item["name"] == fixture_name)
    models = {model.__name__: model for model in PUBLIC_MODELS}
    payload = json.loads(case["json"])
    if fixture_name == "schema_version_boolean_true":
        assert type(payload["schema_version"]) is bool
    else:
        assert type(payload["data"]["response"]["coverage"]["counts_disclosed"]) is int

    with pytest.raises(InvalidContract):
        parse_contract(models[case["model"]], case["json"].encode("utf-8"))


@pytest.mark.parametrize("case", shared_fixture_cases(), ids=lambda case: case["name"])
def test_shared_python_and_frontend_contract_fixtures(case):
    models = {model.__name__: model for model in PUBLIC_MODELS}
    assert case["model"] in models
    model = models[case["model"]]
    raw = case["json"].encode("utf-8")

    if case["valid"]:
        parsed = parse_contract(model, raw)
        assert isinstance(parsed, model)
    else:
        with pytest.raises(InvalidContract):
            parse_contract(model, raw)


def test_openapi_export_is_offline_empty_path_and_public_only():
    document = public_openapi()
    schemas = document["components"]["schemas"]
    expected_components = {
        "AnswerPeriod",
        "AnswerScope",
        "ApiError",
        "Bindings",
        "ChatResultEnvelope",
        "ChatTurnData",
        "Claim",
        "Column",
        "Coverage",
        "CreateChatRequest",
        "Dataset",
        "ErrorCode",
        "ErrorEnvelope",
        "Evidence",
        "Fact",
        "MetricRow",
        "Source",
        "StructuredResponse",
        "TurnRequest",
        "Visual",
    }
    assert document["paths"] == {}
    assert set(schemas) == expected_components
    assert set(document["x-error-catalog"]) == {code.value for code in ERROR_CATALOG}
    for name, schema in schemas.items():
        properties = schema.get("properties", {})
        required = set(schema.get("required", ()))
        optional = {"commit_state"} if name == "ApiError" else set()
        if properties:
            assert required == set(properties) - optional
    assert {
        "schema_version",
        "request_id",
        "error",
    } <= set(schemas["ErrorEnvelope"]["required"])
    serialized = json.dumps(document).casefold()
    for internal_marker in (
        "access_token",
        "refresh_token",
        "password",
        "database_uri",
        "driver",
        "trace_id",
    ):
        assert internal_marker not in serialized
    assert OPENAPI_PATH.is_file()
    assert OPENAPI_PATH.read_text(encoding="utf-8") == export_text()


def error_test_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.get("/probe/{case}")
    async def probe(case: str):
        marker = "private-input-marker"
        if case == "access":
            raise AccessDenied(marker)
        if case == "identity":
            raise IdentityDenied(marker)
        if case == "source":
            raise SourceUnavailable(f"mongodb://user:secret@db.invalid/{marker}")
        if case == "cleanup-pending":
            raise SavedChatCleanupPending(marker)
        if case == "api-problem":
            raise ApiProblem(ErrorCode.PERSISTENCE_FAILED, commit_state="unknown")
        if case == "http-not-found":
            raise HTTPException(status_code=404, detail={"reason": marker})
        if case == "http-unknown-status":
            raise HTTPException(status_code=418, detail=marker)
        if case == "unknown":
            raise RuntimeError(f"internal-stack-sentinel {marker}")
        return {"ok": True}

    @app.post("/request-validation")
    async def request_validation(body: CreateChatRequest):
        return body

    @app.get("/response-validation", response_model=CreateChatRequest)
    async def response_validation():
        return {"request_id": "private-input-marker", "driver": "private-input-marker"}

    @app.get("/stream-failure")
    async def stream_failure():
        async def chunks():
            yield b"first chunk"
            raise RuntimeError("private-stream-sentinel")

        return StreamingResponse(chunks())

    return app


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("access", ErrorCode.ACCESS_DENIED),
        ("identity", ErrorCode.AUTHENTICATION_REQUIRED),
        ("source", ErrorCode.SOURCE_UNAVAILABLE),
        ("cleanup-pending", ErrorCode.CLEANUP_PENDING),
        ("api-problem", ErrorCode.PERSISTENCE_FAILED),
        ("http-not-found", ErrorCode.NOT_FOUND),
        ("http-unknown-status", ErrorCode.INTERNAL_ERROR),
        ("unknown", ErrorCode.INTERNAL_ERROR),
    ],
)
@pytest.mark.asyncio
async def test_http_error_handlers_return_only_fixed_sanitized_envelopes(case, code):
    app = error_test_app()
    attacker_request_id = "a5b04ef6-5713-487f-8995-8c5eb02d4b74"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test.invalid"
    ) as client:
        response = await client.get(
            f"/probe/{case}", headers={"X-Request-ID": attacker_request_id}
        )

    status, message, retryable = ERROR_CATALOG[code]
    body = response.json()
    assert response.status_code == status
    assert body["schema_version"] == 1
    assert UUID(body["request_id"])
    assert body["request_id"] != attacker_request_id
    assert body["error"]["code"] == code.value
    assert body["error"]["message"] == message
    assert body["error"]["retryable"] is retryable
    assert "private-input-marker" not in response.text
    assert "mongodb://" not in response.text
    assert "traceback" not in response.text.casefold()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    if case == "api-problem":
        assert body["error"]["commit_state"] == "unknown"
    else:
        assert "commit_state" not in body["error"]


@pytest.mark.parametrize(
    ("path", "method", "body", "expected_code"),
    [
        (
            "/request-validation",
            "post",
            {"request_id": "bad-id", "token": "private-input-marker"},
            ErrorCode.INVALID_REQUEST,
        ),
        (
            "/response-validation",
            "get",
            None,
            ErrorCode.RESPONSE_INVALID,
        ),
    ],
)
@pytest.mark.asyncio
async def test_fastapi_validation_errors_do_not_echo_input_or_response_values(
    path, method, body, expected_code
):
    app = error_test_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test.invalid"
    ) as client:
        response = await client.request(method.upper(), path, json=body)

    status, message, retryable = ERROR_CATALOG[expected_code]
    assert response.status_code == status
    assert response.json()["error"] == {
        "code": expected_code.value,
        "message": message,
        "retryable": retryable,
    }
    assert "private-input-marker" not in response.text
    assert "bad-id" not in response.text
    assert "token" not in response.text
    assert "driver" not in response.text


@pytest.mark.asyncio
async def test_unexpected_error_returns_safe_500_when_transport_reraises_app_errors():
    app = error_test_app()
    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(
        transport=transport, base_url="http://test.invalid"
    ) as client:
        response = await client.get("/probe/unknown")

    _status, message, retryable = ERROR_CATALOG[ErrorCode.INTERNAL_ERROR]
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": ErrorCode.INTERNAL_ERROR.value,
        "message": message,
        "retryable": retryable,
    }
    assert "internal-stack-sentinel" not in response.text
    assert "private-input-marker" not in response.text
    assert "traceback" not in response.text.casefold()


@pytest.mark.asyncio
async def test_failure_after_response_start_raises_only_a_safe_exception():
    app = error_test_app()
    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(
        transport=transport, base_url="http://test.invalid"
    ) as client:
        with pytest.raises(
            RuntimeError, match="^Response transmission failed\\.$"
        ) as error:
            await client.get("/stream-failure")

    rendered_traceback = "".join(
        traceback.format_exception(
            type(error.value), error.value, error.value.__traceback__
        )
    )
    assert error.value.__suppress_context__ is True
    assert "private-stream-sentinel" not in rendered_traceback
    assert str(error.value) == "Response transmission failed."
