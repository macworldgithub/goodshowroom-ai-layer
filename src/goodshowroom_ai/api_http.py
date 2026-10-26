"""Fixed public error responses. No business endpoint or identity bypass.

Safe error construction never inspects exception messages, request values, paths,
headers or validation error lists. Future handlers still authorize every action.
"""

from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from goodshowroom_ai.api_contracts import (
    ApiError,
    ChatResultEnvelope,
    COMMIT_ERROR_CODES,
    ERROR_CATALOG,
    ErrorCode,
    ErrorEnvelope,
    InvalidContract,
    parse_contract,
)
from goodshowroom_ai.authorization import AccessDenied
from goodshowroom_ai.execution import (
    MAX_REQUEST_BYTES,
    ExecutionLimitExceeded,
)
from goodshowroom_ai.identity import IdentityDenied
from goodshowroom_ai.operational import SourceUnavailable
from goodshowroom_ai.responses import ResponseInvalid
from goodshowroom_ai.storage.mongo import (
    AppDatabaseUnavailable,
    AppWriteConflict,
    AuthorizationDenied,
    SavedChatCleanupPending,
)

SAFE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ApiProblem(Exception):
    """A trusted service selects a code, never a public message or status."""

    def __init__(self, code: ErrorCode, *, commit_state: str | None = None):
        if not isinstance(code, ErrorCode) or (
            commit_state is not None
            and (
                code not in COMMIT_ERROR_CODES
                or commit_state not in {"not_committed", "unknown"}
            )
        ):
            raise ValueError("Invalid API problem")
        self.code, self.commit_state = code, commit_state
        super().__init__("API request failed.")


def error_response(
    code: ErrorCode, *, request_id: UUID, commit_state: str | None = None
) -> JSONResponse:
    status, message, retryable = ERROR_CATALOG[code]
    envelope = ErrorEnvelope(
        request_id=request_id,
        error=ApiError(
            code=code, message=message, retryable=retryable, commit_state=commit_state
        ),
    )
    headers = dict(SAFE_HEADERS)
    if code is ErrorCode.RATE_LIMITED:
        headers["Retry-After"] = "60"
    elif code is ErrorCode.REQUEST_IN_PROGRESS:
        headers["Retry-After"] = "1"
    return JSONResponse(
        status_code=status,
        headers=headers,
        content=envelope.model_dump(mode="json", exclude_none=True),
    )


def success_response(envelope: ChatResultEnvelope) -> JSONResponse:
    """Validate the full wire envelope; caller must prove authorization/commit.

    The persistence string is a contract, not evidence that a commit occurred.
    Do not call this helper until the corresponding mode's completion is verified.
    """
    try:
        if not isinstance(envelope, ChatResultEnvelope):
            raise ValueError()
        checked = parse_contract(
            ChatResultEnvelope,
            envelope.model_dump_json(warnings="error").encode("utf-8"),
        )
        return JSONResponse(
            headers=SAFE_HEADERS, content=checked.model_dump(mode="json")
        )
    except Exception:
        raise ResponseInvalid("Response is invalid.") from None


def _request_id(request: Request) -> UUID:
    # This private state can later be assigned from a successfully validated body
    # by the fixed route. An arbitrary X-Request-ID header is never trusted.
    value = getattr(request.state, "contract_request_id", None)
    if not isinstance(value, UUID):
        value = uuid4()
        request.state.contract_request_id = value
    return value


class _SafeFailureBoundary:
    """Bound raw request bytes and stop errors before raw server logging."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            content_lengths = [
                value
                for name, value in scope.get("headers", ())
                if name.lower() == b"content-length"
            ]
            if len(content_lengths) > 1:
                response = error_response(ErrorCode.INVALID_REQUEST, request_id=uuid4())
                await response(scope, receive, safe_send)
                return
            if content_lengths:
                raw_length = content_lengths[0]
                if not raw_length or not raw_length.isdigit():
                    response = error_response(
                        ErrorCode.INVALID_REQUEST, request_id=uuid4()
                    )
                    await response(scope, receive, safe_send)
                    return
                if len(raw_length) > 5 or int(raw_length) > MAX_REQUEST_BYTES:
                    response = error_response(
                        ErrorCode.PAYLOAD_TOO_LARGE, request_id=uuid4()
                    )
                    await response(scope, receive, safe_send)
                    return

            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    raise RuntimeError("Invalid request transport event.")
                chunk = message.get("body", b"")
                if type(chunk) is not bytes:
                    response = error_response(
                        ErrorCode.INVALID_REQUEST, request_id=uuid4()
                    )
                    await response(scope, receive, safe_send)
                    return
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    response = error_response(
                        ErrorCode.PAYLOAD_TOO_LARGE, request_id=uuid4()
                    )
                    await response(scope, receive, safe_send)
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break

            sent_body = False

            async def replay_body():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            await self.app(scope, replay_body, safe_send)
        except Exception:
            if started:
                # Never send a second response. Later turn endpoints must build
                # and validate a complete body before starting its transmission.
                raise RuntimeError("Response transmission failed.") from None
            response = error_response(
                ErrorCode.INTERNAL_ERROR, request_id=_request_id(Request(scope))
            )
            await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    app.add_middleware(_SafeFailureBoundary)

    async def handle(request: Request, error: Exception):
        commit_state = None
        if isinstance(error, ApiProblem):
            code, commit_state = error.code, error.commit_state
        elif isinstance(error, (RequestValidationError, InvalidContract)):
            code = ErrorCode.INVALID_REQUEST
        elif isinstance(error, (ResponseValidationError, ResponseInvalid)):
            code = ErrorCode.RESPONSE_INVALID
        elif isinstance(error, ExecutionLimitExceeded):
            code = ErrorCode.RESPONSE_INVALID
        elif isinstance(error, IdentityDenied):
            code = ErrorCode.AUTHENTICATION_REQUIRED
        elif isinstance(error, (AccessDenied, AuthorizationDenied)):
            code = ErrorCode.ACCESS_DENIED
        elif isinstance(error, SourceUnavailable):
            code = ErrorCode.SOURCE_UNAVAILABLE
        elif isinstance(error, SavedChatCleanupPending):
            code = ErrorCode.CLEANUP_PENDING
        elif isinstance(error, AppDatabaseUnavailable):
            code = ErrorCode.PERSISTENCE_FAILED
        elif isinstance(error, AppWriteConflict):
            code = ErrorCode.VERSION_CONFLICT
        elif isinstance(error, HTTPException):
            code = {
                401: ErrorCode.AUTHENTICATION_REQUIRED,
                403: ErrorCode.ACCESS_DENIED,
                404: ErrorCode.NOT_FOUND,
                405: ErrorCode.METHOD_NOT_ALLOWED,
                409: ErrorCode.VERSION_CONFLICT,
                410: ErrorCode.TEMPORARY_UNAVAILABLE,
                413: ErrorCode.PAYLOAD_TOO_LARGE,
                422: ErrorCode.INVALID_REQUEST,
                429: ErrorCode.RATE_LIMITED,
                502: ErrorCode.MODEL_FAILURE,
                503: ErrorCode.SOURCE_UNAVAILABLE,
                504: ErrorCode.DEADLINE_EXCEEDED,
            }.get(error.status_code, ErrorCode.INTERNAL_ERROR)
        else:
            code = ErrorCode.INTERNAL_ERROR
        return error_response(
            code, request_id=_request_id(request), commit_state=commit_state
        )

    for error_type in (
        ApiProblem,
        RequestValidationError,
        ResponseValidationError,
        InvalidContract,
        IdentityDenied,
        AccessDenied,
        AuthorizationDenied,
        SourceUnavailable,
        SavedChatCleanupPending,
        AppDatabaseUnavailable,
        AppWriteConflict,
        ResponseInvalid,
        HTTPException,
        Exception,
    ):
        app.add_exception_handler(error_type, handle)
