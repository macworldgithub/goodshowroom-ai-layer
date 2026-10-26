"""Copy-only, metadata-only native SDK export for the pinned Agents SDK.

No live destination is configured here. Trusted startup may supply the native
exporter only after provider access/retention review. Tests use its real JSON
encoder with an in-memory HTTP transport. Unknown SDK shapes fail closed.
"""

import json
import logging
import math
import queue
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from uuid import UUID

from agents.tracing.processor_interface import TracingExporter, TracingProcessor
from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor

MAX_BATCH_ITEMS = 32
MAX_ITEM_BYTES = 16_384
MAX_BATCH_BYTES = MAX_BATCH_ITEMS * MAX_ITEM_BYTES
EXPORT_SECONDS = 2.0
_ENDPOINT = "https://api.openai.com/v1/traces/ingest"
_WORKFLOWS = frozenset({"GoodShowroom.SavedChat", "GoodShowroom.TemporaryChat"})
_STAGES = frozenset(
    {
        "authorization",
        "context_preparation",
        "argument_validation",
        "tool_execution",
        "data_validation",
        "response_construction",
        "final_policy_check",
        "persistence",
        "memory",
        "response_validation",
    }
)
_ERRORS = frozenset(
    {
        "authorization_denied",
        "validation_failed",
        "database_failed",
        "model_failed",
        "response_invalid",
        "persistence_failed",
        "timeout",
        "cancelled",
        "execution_failed",
    }
)
_OUTCOMES = frozenset(
    {"success", "denied", "failed", "cancelled", "timeout", "unavailable"}
)
_logger = logging.getLogger(__name__)


class _SafeSDKLogHandler(logging.Handler):
    def emit(self, record):
        # Emit a new record, without raw message/args/extra/exception state.
        _logger.warning("tracing_sdk_diagnostic")


def protect_sdk_logs() -> None:
    """Suppress SDK and native transport content before export can execute."""
    for name in ("openai.agents", "httpx2", "httpcore2"):
        sdk_logger = logging.getLogger(name)
        sdk_logger.handlers = [_SafeSDKLogHandler()]
        sdk_logger.propagate = False
        sdk_logger.setLevel(logging.WARNING)


class UnsafeTracePayload(ValueError):
    def __init__(self):
        super().__init__("Unsafe trace payload")


def _identifier(value, prefix: str, size: int) -> str:
    if type(value) is not str or not re.fullmatch(
        prefix + rf"[0-9a-f]{{{size}}}", value
    ):
        raise UnsafeTracePayload()
    return value


def _diagnostic_id(value) -> str:
    value = _identifier(value, "", 32)
    if (
        UUID(hex=value).version != 4
        or UUID(hex=value).variant != "specified in RFC 4122"
    ):
        raise UnsafeTracePayload()
    return value


def _usage(value) -> dict:
    if value is None:
        return {}
    if type(value) is not dict:
        raise UnsafeTracePayload()
    result = {}
    for key in ("input_tokens", "output_tokens"):
        if key in value:
            number = value[key]
            if type(number) is not int or not 0 <= number <= 2_147_483_647:
                raise UnsafeTracePayload()
            result[key] = number
    return result


def _metadata(value) -> dict:
    if value is None:
        return {}
    if type(value) is not dict:
        raise UnsafeTracePayload()
    return (
        {"diagnostic_request_id": _diagnostic_id(value["diagnostic_request_id"])}
        if "diagnostic_request_id" in value
        else {}
    )


def _timestamp(value) -> tuple[str, datetime]:
    if type(value) is not str or len(value) > 40:
        raise UnsafeTracePayload()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise UnsafeTracePayload()
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(), parsed
    except (ValueError, OverflowError):
        raise UnsafeTracePayload() from None


def _span_data(value) -> dict:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise UnsafeTracePayload()
    kind = value["type"]
    if kind in {"generation", "response"}:
        # Responses IDs would allow fetching content through another surface.
        # Use the native generation schema with usage only; no provider IDs.
        return {"type": "generation", "usage": _usage(value.get("usage"))}
    if kind == "agent":
        if (
            type(value.get("name")) is not str
            or value["name"] != "GoodShowroom.PrimaryAgent"
        ):
            raise UnsafeTracePayload()
        return {"type": "agent", "name": "GoodShowroom.PrimaryAgent"}
    if kind == "function":
        if type(value.get("name")) is not str or value["name"] != "count_leads":
            raise UnsafeTracePayload()
        return {
            "type": "function",
            "name": "count_leads",
            "input": None,
            "output": None,
        }
    if kind == "guardrail":
        if type(value.get("name")) is not str or value["name"] not in {
            "authorization",
            "data_validation",
            "response_validation",
        }:
            raise UnsafeTracePayload()
        if type(value.get("triggered")) is not bool:
            raise UnsafeTracePayload()
        return {"type": kind, "name": value["name"], "triggered": value["triggered"]}
    if kind != "custom" or type(value.get("name")) is not str:
        raise UnsafeTracePayload()
    name = value["name"]
    data = value.get("data")
    if type(data) is not dict:
        raise UnsafeTracePayload()
    if name in {"task", "turn"}:
        if data.get("sdk_span_type") != name:
            raise UnsafeTracePayload()
        return {
            "type": "custom",
            "name": name,
            "data": {"usage": _usage(data.get("usage"))},
        }
    if name not in _STAGES:
        raise UnsafeTracePayload()
    safe = _usage(data)
    for key, choices in (("outcome", _OUTCOMES), ("error_category", _ERRORS)):
        if key in data:
            if type(data[key]) is not str or data[key] not in choices:
                raise UnsafeTracePayload()
            safe[key] = data[key]
    if "duration_ms" in data:
        duration = data["duration_ms"]
        if (
            type(duration) not in {int, float}
            or not 0 <= duration <= 86_400_000
            or not math.isfinite(duration)
        ):
            raise UnsafeTracePayload()
        safe["duration_ms"] = duration
    return {"type": "custom", "name": name, "data": safe}


def sanitize_payload(payload: dict) -> dict:
    """Construct a new fixed-depth object; never traverse unknown data."""
    if type(payload) is not dict:
        raise UnsafeTracePayload()
    kind = payload.get("object")
    if type(kind) is not str:
        raise UnsafeTracePayload()
    if kind == "trace":
        if (
            type(payload.get("workflow_name")) is not str
            or payload["workflow_name"] not in _WORKFLOWS
        ):
            raise UnsafeTracePayload()
        result = {
            "object": "trace",
            "id": _identifier(payload.get("id"), "trace_", 32),
            "workflow_name": payload["workflow_name"],
            "metadata": _metadata(payload.get("metadata")),
        }
        if payload.get("group_id") is not None:
            result["group_id"] = _diagnostic_id(payload["group_id"])
    elif kind == "trace.span":
        start, start_time = _timestamp(payload.get("started_at"))
        end, end_time = _timestamp(payload.get("ended_at"))
        if end_time < start_time:
            raise UnsafeTracePayload()
        result = {
            "object": "trace.span",
            "id": _identifier(payload.get("id"), "span_", 24),
            "trace_id": _identifier(payload.get("trace_id"), "trace_", 32),
            "parent_id": None
            if payload.get("parent_id") is None
            else _identifier(payload["parent_id"], "span_", 24),
            "started_at": start,
            "ended_at": end,
            "span_data": _span_data(payload.get("span_data")),
            "metadata": _metadata(payload.get("metadata")),
            "error": None,
        }
        if payload.get("error") is not None:
            error = payload["error"]
            if type(error) is not dict:
                raise UnsafeTracePayload()
            details = error.get("data")
            category = details.get("category") if type(details) is dict else None
            if type(category) is not str or category not in _ERRORS:
                category = "execution_failed"
            result["error"] = {
                "message": "operation_failed",
                "data": {"category": category},
            }
    else:
        raise UnsafeTracePayload()
    # Serialize ONLY the newly built object. No default=str/raw fallback.
    try:
        encoded = json.dumps(result, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_ITEM_BYTES:
            raise UnsafeTracePayload()
    except (TypeError, ValueError, OverflowError):
        raise UnsafeTracePayload() from None
    return result


@dataclass(frozen=True, slots=True)
class _FrozenItem:
    payload: bytes
    tracing_api_key: None = None

    def export(self) -> dict:
        return json.loads(self.payload)


class SanitizingExporter(TracingExporter):
    """Only safe copies reach the real native exporter's JSON encoder."""

    def __init__(self, native: BackendSpanExporter):
        protect_sdk_logs()
        if type(native) is not BackendSpanExporter or native.endpoint != _ENDPOINT:
            raise ValueError("Native trace destination is not permitted")
        self._native = native
        self._stopped = False
        native.max_retries = 1

    def snapshot(self, item) -> _FrozenItem | None:
        try:
            safe = sanitize_payload(item.export())
            return _FrozenItem(json.dumps(safe, allow_nan=False).encode("utf-8"))
        except Exception:
            _logger.warning("tracing_payload_dropped")
            return None

    def export(self, items) -> None:
        if self._stopped:
            return
        try:
            if self._native.endpoint != _ENDPOINT:
                raise UnsafeTracePayload()
            self._native.max_retries = 1
            if type(items) is not list or not 0 < len(items) <= MAX_BATCH_ITEMS:
                raise UnsafeTracePayload()
            copies = []
            for item in items:
                snapshot = self.snapshot(item)
                if snapshot is None:
                    return  # Drop the entire batch; no partly trusted fallback.
                copies.append(snapshot)
            if sum(len(item.payload) for item in copies) > MAX_BATCH_BYTES:
                raise UnsafeTracePayload()
            # Exact pinned class only; preserve a total export deadline, no retries.
            # Per-item tracing_api_key is always None: server-owned key routing only.
            self._native._export_with_deadline(
                copies, deadline=monotonic() + EXPORT_SECONDS
            )
        except Exception:
            _logger.warning("tracing_export_failed")

    def _request_shutdown(self) -> None:
        self._stopped = True
        self._native._request_shutdown()

    def close(self) -> None:
        self._stopped = True
        try:
            self._native.close()
        except Exception:
            _logger.warning("tracing_export_failed")


class _BoundedBatchProcessor(BatchTraceProcessor):
    def _run(self):
        try:
            super()._run()
        except Exception:
            _logger.warning("tracing_diagnostic_failure")

    def force_flush(self):
        self._export_batches(deadline=monotonic() + EXPORT_SECONDS)


class SanitizedTraceProcessor(TracingProcessor):
    """Freeze safe items BEFORE the native SDK queue retains them."""

    def __init__(self, native: BackendSpanExporter):
        self._exporter = SanitizingExporter(native)
        self._batch = _BoundedBatchProcessor(
            self._exporter,
            max_queue_size=256,
            max_batch_size=MAX_BATCH_ITEMS,
            schedule_delay=1.0,
        )
        self._closed = False

    def _enqueue(self, item, *, span: bool) -> None:
        if self._closed:
            return
        snapshot = self._exporter.snapshot(item)
        if snapshot is not None:
            try:
                if span:
                    self._batch.on_span_end(snapshot)
                else:
                    self._batch.on_trace_start(snapshot)
            except Exception:
                _logger.warning("tracing_diagnostic_failure")

    def on_trace_start(self, trace):
        self._enqueue(trace, span=False)

    def on_trace_end(self, trace):
        pass

    def on_span_start(self, span):
        pass

    def on_span_end(self, span):
        self._enqueue(span, span=True)

    def force_flush(self):
        if self._closed:
            return
        try:
            self._batch.force_flush()
        except Exception:
            _logger.warning("tracing_diagnostic_failure")

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._batch.shutdown(timeout=EXPORT_SECONDS)
        except Exception:
            _logger.warning("tracing_diagnostic_failure")
        finally:
            # Bound the drain to the native queue capacity. No raw items persist.
            for _ in range(256):
                try:
                    self._batch._queue.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    _logger.warning("tracing_diagnostic_failure")
                    break
            self._exporter.close()
