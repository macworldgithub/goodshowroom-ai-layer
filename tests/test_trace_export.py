"""Offline privacy contracts for sanitized native trace export."""

import json
import logging
from dataclasses import FrozenInstanceError
from time import monotonic
from uuid import uuid4

import httpx2
import pytest
from agents.tracing import custom_span
from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor

from goodshowroom_ai.config import Settings
from goodshowroom_ai.trace_export import (
    MAX_BATCH_BYTES,
    MAX_BATCH_ITEMS,
    MAX_ITEM_BYTES,
    SanitizedTraceProcessor,
    SanitizingExporter,
    UnsafeTracePayload,
    sanitize_payload,
)
from goodshowroom_ai.tracing import configure_tracing

SENTINEL = f"customer-secret-{uuid4().hex}@example.test"
SENTINEL_URL = f"https://private.example.test/{uuid4().hex}"
SYNTHETIC_API_KEY = f"synthetic-key-{uuid4().hex}"
SYNTHETIC_ITEM_KEY = f"synthetic-item-key-{uuid4().hex}"
SAFE_FAILURE_MESSAGES = {
    "tracing_payload_dropped",
    "tracing_export_failed",
    "tracing_diagnostic_failure",
    "tracing_sdk_diagnostic",
}


def settings(*, app_env="development", enabled=True):
    return Settings(
        _env_file=None,
        app_env=app_env,
        openai_tracing_enabled=enabled,
        openai_tracing_include_sensitive_data=False,
    )


def trace_payload(**overrides):
    payload = {
        "object": "trace",
        "id": f"trace_{uuid4().hex}",
        "workflow_name": "GoodShowroom.SavedChat",
        "group_id": uuid4().hex,
        "metadata": {"diagnostic_request_id": uuid4().hex},
    }
    payload.update(overrides)
    return payload


def span_payload(span_data=None, **overrides):
    payload = {
        "object": "trace.span",
        "id": f"span_{uuid4().hex[:24]}",
        "trace_id": f"trace_{uuid4().hex}",
        "parent_id": None,
        "started_at": "2026-10-26T07:00:00+00:00",
        "ended_at": "2026-10-26T07:00:01+00:00",
        "span_data": span_data
        or {"type": "custom", "name": "authorization", "data": {}},
        "metadata": {"diagnostic_request_id": uuid4().hex},
        "error": None,
    }
    payload.update(overrides)
    return payload


class PayloadItem:
    """Small SDK-shaped item for testing the copy boundary without network I/O."""

    def __init__(self, payload, tracing_api_key=None):
        self.payload = payload
        self.tracing_api_key = tracing_api_key

    def export(self):
        return self.payload


class KeyAccessForbiddenItem:
    def __init__(self, payload):
        self.payload = payload

    @property
    def tracing_api_key(self):
        raise AssertionError("per-item trace keys must not be read")

    def export(self):
        return self.payload


def make_native(transport=None, **kwargs):
    native = BackendSpanExporter(
        api_key=SYNTHETIC_API_KEY,
        organization="synthetic-org",
        project="synthetic-project",
        **kwargs,
    )
    native._client.close()
    native._client = httpx2.Client(
        transport=transport or httpx2.MockTransport(lambda r: httpx2.Response(200))
    )
    return native


def serialized_requests(handler):
    requests = []

    def capture(request):
        requests.append(request)
        return handler(request)

    return requests, httpx2.MockTransport(capture)


def test_trace_payload_keeps_only_static_workflow_and_opaque_metadata():
    request_id = uuid4().hex
    group_id = uuid4().hex
    raw = trace_payload(
        workflow_name="GoodShowroom.TemporaryChat",
        group_id=group_id,
        metadata={"diagnostic_request_id": request_id, "customer_email": SENTINEL},
        raw_chat=SENTINEL,
        nested={"deep": {"secret": SENTINEL}},
    )

    safe = sanitize_payload(raw)

    assert safe == {
        "object": "trace",
        "id": raw["id"],
        "workflow_name": "GoodShowroom.TemporaryChat",
        "group_id": group_id,
        "metadata": {"diagnostic_request_id": request_id},
    }
    assert safe is not raw
    assert SENTINEL not in json.dumps(safe)
    raw["workflow_name"] = "changed-after-copy"
    assert safe["workflow_name"] == "GoodShowroom.TemporaryChat"


def test_unknown_deep_fields_are_not_walked_or_copied():
    cycle = {}
    cycle["self"] = cycle
    raw = trace_payload(
        unknown={"very": {"deep": [SENTINEL, cycle]}},
        metadata={"diagnostic_request_id": uuid4().hex, "untrusted": cycle},
    )

    safe = sanitize_payload(raw)

    assert set(safe) == {"object", "id", "workflow_name", "group_id", "metadata"}
    assert safe["metadata"] == {
        "diagnostic_request_id": raw["metadata"]["diagnostic_request_id"]
    }


@pytest.mark.parametrize(
    ("kind", "raw_data", "expected"),
    [
        (
            "agent",
            {
                "type": "agent",
                "name": "GoodShowroom.PrimaryAgent",
                "tools": ["count_leads", SENTINEL],
                "handoffs": [SENTINEL],
                "output_type": SENTINEL,
                "metadata": {"secret": SENTINEL},
            },
            {"type": "agent", "name": "GoodShowroom.PrimaryAgent"},
        ),
        (
            "function",
            {
                "type": "function",
                "name": "count_leads",
                "input": SENTINEL,
                "output": SENTINEL,
                "mcp_data": {"arguments": SENTINEL},
            },
            {"type": "function", "name": "count_leads", "input": None, "output": None},
        ),
        (
            "generation",
            {
                "type": "generation",
                "input": [{"content": SENTINEL}],
                "output": [{"content": SENTINEL}],
                "model": SENTINEL,
                "model_config": {"api_key": SENTINEL},
                "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            },
            {"type": "generation", "usage": {"input_tokens": 11, "output_tokens": 4}},
        ),
        (
            "response",
            {
                "type": "response",
                "response_id": SENTINEL,
                "input": SENTINEL,
                "usage": {"input_tokens": 8, "output_tokens": 3, "internal": SENTINEL},
            },
            {"type": "generation", "usage": {"input_tokens": 8, "output_tokens": 3}},
        ),
        (
            "custom",
            {
                "type": "custom",
                "name": "tool_execution",
                "data": {
                    "outcome": "success",
                    "error_category": "database_failed",
                    "duration_ms": 22.5,
                    "input_tokens": 6,
                    "output_tokens": 2,
                    "query": SENTINEL,
                    "response_url": SENTINEL_URL,
                },
            },
            {
                "type": "custom",
                "name": "tool_execution",
                "data": {
                    "outcome": "success",
                    "error_category": "database_failed",
                    "duration_ms": 22.5,
                    "input_tokens": 6,
                    "output_tokens": 2,
                },
            },
        ),
        (
            "task",
            {
                "type": "custom",
                "name": "task",
                "data": {
                    "sdk_span_type": "task",
                    "name": SENTINEL,
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                    "metadata": {"secret": SENTINEL},
                },
            },
            {
                "type": "custom",
                "name": "task",
                "data": {"usage": {"input_tokens": 2, "output_tokens": 1}},
            },
        ),
        (
            "turn",
            {
                "type": "custom",
                "name": "turn",
                "data": {
                    "sdk_span_type": "turn",
                    "turn": 2,
                    "agent_name": SENTINEL,
                    "usage": {"input_tokens": 5},
                },
            },
            {"type": "custom", "name": "turn", "data": {"usage": {"input_tokens": 5}}},
        ),
        (
            "guardrail",
            {
                "type": "guardrail",
                "name": "authorization",
                "triggered": True,
                "private": SENTINEL,
            },
            {"type": "guardrail", "name": "authorization", "triggered": True},
        ),
    ],
)
def test_span_data_uses_only_safe_schema(kind, raw_data, expected):
    safe = sanitize_payload(span_payload(span_data=raw_data))

    assert safe["span_data"] == expected
    assert SENTINEL not in json.dumps(safe)
    assert SENTINEL_URL not in json.dumps(safe)


def test_span_fields_and_error_are_copied_to_fixed_safe_schema():
    request_id = uuid4().hex
    span_id = f"span_{uuid4().hex[:24]}"
    raw = span_payload(
        id=span_id,
        started_at="2026-10-26T10:00:00+03:00",
        ended_at="2026-10-26T10:00:02+03:00",
        parent_id=f"span_{uuid4().hex[:24]}",
        metadata={"diagnostic_request_id": request_id, "email": SENTINEL},
        error={
            "message": SENTINEL,
            "data": {"category": "database_failed", "stack": SENTINEL},
        },
        extra={"body": SENTINEL},
    )

    safe = sanitize_payload(raw)

    assert safe == {
        "object": "trace.span",
        "id": span_id,
        "trace_id": raw["trace_id"],
        "parent_id": raw["parent_id"],
        "started_at": "2026-10-26T07:00:00+00:00",
        "ended_at": "2026-10-26T07:00:02+00:00",
        "span_data": {"type": "custom", "name": "authorization", "data": {}},
        "metadata": {"diagnostic_request_id": request_id},
        "error": {
            "message": "operation_failed",
            "data": {"category": "database_failed"},
        },
    }
    assert SENTINEL not in json.dumps(safe)


def test_error_with_unknown_category_uses_safe_fallback():
    raw = span_payload(
        error={"message": SENTINEL, "data": {"category": "customer_record_missing"}}
    )

    safe = sanitize_payload(raw)

    assert safe["error"] == {
        "message": "operation_failed",
        "data": {"category": "execution_failed"},
    }


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"object": "unknown"},
        trace_payload(workflow_name="CustomerChat"),
        trace_payload(id="trace_not-an-id"),
        trace_payload(group_id="a" * 32),
        trace_payload(metadata={"diagnostic_request_id": "customer-123"}),
        span_payload(id="span_short"),
        span_payload(trace_id="trace_short"),
        span_payload(parent_id="span_bad"),
        span_payload(started_at="2026-10-26T07:00:00"),
        span_payload(ended_at="not-a-timestamp"),
        span_payload(
            started_at="2026-10-26T07:00:02Z", ended_at="2026-10-26T07:00:01Z"
        ),
        span_payload(span_data={"type": "handoff", "from_agent": "a", "to_agent": "b"}),
        span_payload(span_data={"type": "agent", "name": SENTINEL}),
        span_payload(span_data={"type": "function", "name": "delete_all"}),
        span_payload(
            span_data={"type": "guardrail", "name": "database", "triggered": True}
        ),
        span_payload(
            span_data={
                "type": "guardrail",
                "name": "authorization",
                "triggered": "true",
            }
        ),
        span_payload(span_data={"type": "custom", "name": "unknown_stage", "data": {}}),
        span_payload(
            span_data={
                "type": "custom",
                "name": "authorization",
                "data": {"outcome": "other"},
            }
        ),
        span_payload(
            span_data={
                "type": "custom",
                "name": "authorization",
                "data": {"error_category": SENTINEL},
            }
        ),
        span_payload(
            span_data={
                "type": "custom",
                "name": "authorization",
                "data": {"duration_ms": float("nan")},
            }
        ),
        span_payload(
            span_data={
                "type": "custom",
                "name": "authorization",
                "data": {"duration_ms": -1},
            }
        ),
        span_payload(
            span_data={
                "type": "custom",
                "name": "authorization",
                "data": {"duration_ms": 10**1000},
            }
        ),
        span_payload(
            span_data={
                "type": "custom",
                "name": "authorization",
                "data": {"duration_ms": True},
            }
        ),
        span_payload(span_data={"type": "generation", "usage": {"input_tokens": True}}),
        span_payload(span_data={"type": "generation", "usage": {"output_tokens": -1}}),
        span_payload(span_data={"type": "generation", "usage": {"input_tokens": 1.5}}),
        span_payload(
            span_data={
                "type": "custom",
                "name": "task",
                "data": {"sdk_span_type": "turn"},
            }
        ),
        span_payload(error="raw error text"),
    ],
)
def test_malformed_or_unknown_safe_fields_fail_closed(raw):
    with pytest.raises(UnsafeTracePayload):
        sanitize_payload(raw)


def test_cyclic_value_in_a_safe_field_fails_closed():
    cycle = {}
    cycle["input_tokens"] = cycle
    raw = span_payload(span_data={"type": "generation", "usage": cycle})

    with pytest.raises(UnsafeTracePayload):
        sanitize_payload(raw)


def test_snapshot_freezes_a_json_copy_before_the_source_changes():
    raw = trace_payload(
        metadata={"diagnostic_request_id": uuid4().hex, "email": SENTINEL}
    )
    source = PayloadItem(raw)
    native = make_native()
    exporter = SanitizingExporter(native)
    try:
        frozen = exporter.snapshot(source)
        assert frozen is not None
        assert frozen.export()["metadata"] == {
            "diagnostic_request_id": raw["metadata"]["diagnostic_request_id"]
        }
        assert len(frozen.payload) <= MAX_ITEM_BYTES
        raw["id"] = "changed-after-snapshot"
        raw["metadata"]["diagnostic_request_id"] = uuid4().hex
        assert frozen.export()["id"] != raw["id"]
        assert SENTINEL not in frozen.payload.decode()
        with pytest.raises(FrozenInstanceError):
            frozen.payload = b"changed"
    finally:
        exporter.close()


def test_invalid_snapshot_is_dropped_with_only_fixed_log(caplog):
    native = make_native()
    exporter = SanitizingExporter(native)
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.trace_export"):
            assert (
                exporter.snapshot(
                    PayloadItem({"object": "unknown", "secret": SENTINEL})
                )
                is None
            )
        assert SENTINEL not in caplog.text
        assert [record.message for record in caplog.records] == [
            "tracing_payload_dropped"
        ]
    finally:
        exporter.close()


@pytest.mark.parametrize("failure", ["sanitize", "json"])
def test_sanitizer_and_json_failures_drop_without_native_fallback(
    failure, monkeypatch, caplog
):
    from goodshowroom_ai import trace_export

    calls = []
    native = make_native()
    monkeypatch.setattr(
        native,
        "_export_with_deadline",
        lambda items, deadline: calls.append((items, deadline)),
    )
    if failure == "sanitize":

        def fail_sanitizer(_payload):
            raise RuntimeError(SENTINEL)

        monkeypatch.setattr(trace_export, "sanitize_payload", fail_sanitizer)
    else:
        monkeypatch.setattr(
            trace_export,
            "sanitize_payload",
            lambda _payload: {"secret": object(), "raw": SENTINEL},
        )

    exporter = SanitizingExporter(native)
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.trace_export"):
            exporter.export([PayloadItem(trace_payload())])

        assert calls == []
        assert SENTINEL not in caplog.text
        assert [record.message for record in caplog.records] == [
            "tracing_payload_dropped"
        ]
    finally:
        exporter.close()


def test_batch_is_atomic_and_rejects_oversized_item_count(monkeypatch):
    calls = []
    native = make_native()
    monkeypatch.setattr(
        native,
        "_export_with_deadline",
        lambda items, deadline: calls.append((items, deadline)),
    )
    exporter = SanitizingExporter(native)
    try:
        valid = PayloadItem(trace_payload())
        invalid = PayloadItem({"object": "unknown", "raw": SENTINEL})
        exporter.export([valid, invalid])
        assert calls == []

        exporter.export(
            [PayloadItem(trace_payload()) for _ in range(MAX_BATCH_ITEMS + 1)]
        )
        assert calls == []
    finally:
        exporter.close()


def test_batch_at_item_limit_stays_within_batch_byte_limit(monkeypatch):
    calls = []
    native = make_native()
    monkeypatch.setattr(
        native,
        "_export_with_deadline",
        lambda items, deadline: calls.append((items, deadline)),
    )
    exporter = SanitizingExporter(native)
    try:
        exporter.export([PayloadItem(trace_payload()) for _ in range(MAX_BATCH_ITEMS)])

        assert len(calls) == 1
        snapshots = calls[0][0]
        assert len(snapshots) == MAX_BATCH_ITEMS
        assert all(len(item.payload) <= MAX_ITEM_BYTES for item in snapshots)
        assert sum(len(item.payload) for item in snapshots) <= MAX_BATCH_BYTES
    finally:
        exporter.close()


def test_native_exporter_gets_only_frozen_copies_with_server_key_routing(monkeypatch):
    calls = []
    native = make_native()
    monkeypatch.setattr(
        native,
        "_export_with_deadline",
        lambda items, deadline: calls.append((items, deadline)),
    )
    exporter = SanitizingExporter(native)
    source = PayloadItem(trace_payload(), tracing_api_key=SYNTHETIC_ITEM_KEY)
    try:
        exporter.export([source])

        assert native.max_retries == 1
        assert len(calls) == 1
        items, deadline = calls[0]
        assert items[0].tracing_api_key is None
        assert items[0].export()["object"] == "trace"
        assert 0 < deadline - monotonic() <= 2.0
        assert SYNTHETIC_ITEM_KEY not in items[0].payload.decode()
    finally:
        exporter.close()


def test_snapshot_never_reads_per_item_tracing_key(monkeypatch):
    calls = []
    native = make_native()
    monkeypatch.setattr(
        native,
        "_export_with_deadline",
        lambda items, deadline: calls.append((items, deadline)),
    )
    exporter = SanitizingExporter(native)
    try:
        exporter.export([KeyAccessForbiddenItem(trace_payload())])

        assert len(calls) == 1
        assert calls[0][0][0].tracing_api_key is None
    finally:
        exporter.close()


def test_native_http_serialization_uses_only_allowlisted_payload_and_fixed_route(
    monkeypatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    requests, transport = serialized_requests(
        lambda request: httpx2.Response(200, request=request)
    )
    native = make_native(transport)
    exporter = SanitizingExporter(native)
    trace = trace_payload(
        metadata={"diagnostic_request_id": uuid4().hex, "email": SENTINEL},
        customer=SENTINEL,
    )
    generation = span_payload(
        span_data={
            "type": "generation",
            "input": [{"content": SENTINEL}],
            "output": [{"content": SENTINEL}],
            "model": SENTINEL,
            "model_config": {"api_key": SENTINEL},
            "usage": {"input_tokens": 17, "output_tokens": 5},
        },
        error={
            "message": SENTINEL,
            "data": {"category": "model_failed", "stack": SENTINEL},
        },
    )
    response = span_payload(
        span_data={
            "type": "response",
            "response_id": SENTINEL,
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
    )
    agent = span_payload(
        span_data={
            "type": "agent",
            "name": "GoodShowroom.PrimaryAgent",
            "tools": ["count_leads", SENTINEL],
        }
    )
    function = span_payload(
        span_data={
            "type": "function",
            "name": "count_leads",
            "input": SENTINEL,
            "output": SENTINEL,
            "mcp_data": {"url": SENTINEL_URL},
        }
    )
    custom = span_payload(
        span_data={
            "type": "custom",
            "name": "response_validation",
            "data": {"outcome": "success", "response_url": SENTINEL_URL},
        }
    )

    try:
        exporter.export(
            [
                PayloadItem(trace, tracing_api_key=SYNTHETIC_ITEM_KEY),
                PayloadItem(agent, tracing_api_key=SYNTHETIC_ITEM_KEY),
                PayloadItem(function, tracing_api_key=SYNTHETIC_ITEM_KEY),
                PayloadItem(generation, tracing_api_key=SYNTHETIC_ITEM_KEY),
                PayloadItem(response, tracing_api_key=SYNTHETIC_ITEM_KEY),
                PayloadItem(custom, tracing_api_key=SYNTHETIC_ITEM_KEY),
            ]
        )

        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "https://api.openai.com/v1/traces/ingest"
        assert request.headers["Authorization"] == f"Bearer {SYNTHETIC_API_KEY}"
        assert request.headers["OpenAI-Beta"] == "traces=v1"
        assert request.headers["OpenAI-Organization"] == "synthetic-org"
        assert request.headers["OpenAI-Project"] == "synthetic-project"
        body = request.read().decode("utf-8")
        assert SENTINEL not in body
        assert SENTINEL_URL not in body
        assert SYNTHETIC_API_KEY not in body
        assert SYNTHETIC_ITEM_KEY not in body
        data = json.loads(body)["data"]
        assert [item["object"] for item in data] == [
            "trace",
            "trace.span",
            "trace.span",
            "trace.span",
            "trace.span",
            "trace.span",
        ]
        assert data[1]["span_data"] == {
            "type": "agent",
            "name": "GoodShowroom.PrimaryAgent",
        }
        assert data[2]["span_data"] == {
            "type": "function",
            "name": "count_leads",
            "input": None,
            "output": None,
        }
        assert data[3]["span_data"] == {
            "type": "generation",
            "usage": {"input_tokens": 17, "output_tokens": 5},
        }
        assert data[3]["error"] == {
            "message": "operation_failed",
            "data": {"category": "model_failed"},
        }
        assert data[4]["span_data"] == {
            "type": "generation",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
        assert data[5]["span_data"] == {
            "type": "custom",
            "name": "response_validation",
            "data": {"outcome": "success"},
        }
        assert "response_id" not in body
    finally:
        exporter.close()


def test_native_backend_rejects_any_non_official_endpoint():
    native = BackendSpanExporter(
        api_key=SYNTHETIC_API_KEY, endpoint="https://example.test/ingest"
    )
    try:
        with pytest.raises(ValueError, match="destination is not permitted"):
            SanitizingExporter(native)
    finally:
        native.close()


def test_native_export_rechecks_endpoint_before_each_batch(monkeypatch, caplog):
    calls = []
    native = make_native()
    exporter = SanitizingExporter(native)
    monkeypatch.setattr(
        native,
        "_export_with_deadline",
        lambda items, deadline: calls.append((items, deadline)),
    )
    try:
        native.endpoint = "https://private.example.test/ingest"
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.trace_export"):
            exporter.export([PayloadItem(trace_payload())])

        assert calls == []
        assert [record.message for record in caplog.records] == [
            "tracing_export_failed"
        ]
    finally:
        exporter.close()


def test_configured_native_processor_batches_and_closes_without_parallel_export():
    requests, transport = serialized_requests(
        lambda request: httpx2.Response(200, request=request)
    )
    native = make_native(transport)
    runtime = configure_tracing(settings(), native_exporter=native)
    try:
        assert runtime.enabled is True
        assert runtime.export_enabled is True
        with runtime.root_trace("temporary"):
            with custom_span(
                "authorization",
                data={"outcome": "success", "customer": SENTINEL},
            ):
                pass

        runtime.close()

        assert runtime.enabled is False
        assert runtime.export_enabled is False
        assert len(requests) == 1
        body = requests[0].read().decode()
        assert SENTINEL not in body
        assert [item["object"] for item in json.loads(body)["data"]] == [
            "trace",
            "trace.span",
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("app_env", "enabled"),
    [("test", True), ("production", True), ("development", False)],
)
def test_native_export_stays_inactive_when_settings_do_not_enable_tracing(
    app_env, enabled
):
    requests, transport = serialized_requests(
        lambda request: httpx2.Response(200, request=request)
    )
    native = make_native(transport)
    runtime = configure_tracing(
        settings(app_env=app_env, enabled=enabled), native_exporter=native
    )
    try:
        assert runtime.enabled is False
        assert runtime.export_enabled is False
        with runtime.root_trace("saved"):
            with custom_span("authorization", data={"outcome": "success"}):
                pass
        runtime.close()
        assert requests == []
    finally:
        runtime.close()
        native.close()


def test_default_runtime_has_no_native_export_path():
    runtime = configure_tracing(settings())
    try:
        assert runtime.enabled is True
        assert runtime.export_enabled is False
        with runtime.root_trace("saved"):
            with custom_span("authorization", data={"outcome": "success"}):
                pass
    finally:
        runtime.close()


def test_native_and_capture_destinations_cannot_be_attached_together():
    class CaptureProcessor:
        def on_trace_start(self, trace):
            pass

        def on_trace_end(self, trace):
            pass

        def on_span_start(self, span):
            pass

        def on_span_end(self, span):
            pass

        def force_flush(self):
            pass

        def shutdown(self):
            pass

    native = make_native()
    try:
        with pytest.raises(ValueError, match="Multiple trace destinations"):
            configure_tracing(
                settings(), capture_processor=CaptureProcessor(), native_exporter=native
            )
    finally:
        native.close()


def test_processor_queues_only_frozen_safe_items(monkeypatch):
    native = make_native()
    processor = SanitizedTraceProcessor(native)
    queued = []
    monkeypatch.setattr(processor._batch, "on_trace_start", queued.append)
    raw = trace_payload(customer=SENTINEL)
    processor.on_trace_start(PayloadItem(raw))
    try:
        assert len(queued) == 1
        assert queued[0].__class__.__name__ == "_FrozenItem"
        assert queued[0].export()["object"] == "trace"
        assert SENTINEL not in queued[0].payload.decode()
        raw["id"] = "mutated-after-callback"
        assert queued[0].export()["id"] != raw["id"]
    finally:
        processor.shutdown()


def test_sdk_log_handler_hides_provider_body_and_exception_even_with_debug_data_enabled(
    monkeypatch,
):
    import agents._debug as sdk_debug
    from goodshowroom_ai import trace_export

    sdk_logger = logging.getLogger("openai.agents")
    monkeypatch.setattr(sdk_logger, "handlers", list(sdk_logger.handlers))
    monkeypatch.setattr(sdk_logger, "propagate", sdk_logger.propagate)
    monkeypatch.setattr(sdk_logger, "level", sdk_logger.level)
    monkeypatch.setattr(sdk_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(sdk_debug, "DONT_LOG_TOOL_DATA", False)
    messages = []
    monkeypatch.setattr(
        trace_export._logger, "warning", lambda *args, **_kwargs: messages.append(args)
    )
    runtime = configure_tracing(settings())
    try:
        assert sdk_logger.handlers
        assert sdk_logger.handlers[0].__class__.__name__ == "_SafeSDKLogHandler"
        try:
            raise RuntimeError(SENTINEL)
        except RuntimeError:
            sdk_logger.error("provider body %s", SENTINEL, exc_info=True)
        assert messages == [("tracing_sdk_diagnostic",)]
        assert SENTINEL not in repr(messages)
    finally:
        runtime.close()


def test_native_export_failure_does_not_leak_provider_response_or_exception(
    monkeypatch, caplog
):
    requests, transport = serialized_requests(
        lambda request: httpx2.Response(400, content=SENTINEL.encode(), request=request)
    )
    native = make_native(transport)
    runtime = configure_tracing(settings(), native_exporter=native)
    try:
        import agents._debug as sdk_debug

        monkeypatch.setattr(sdk_debug, "DONT_LOG_MODEL_DATA", False)
        monkeypatch.setattr(sdk_debug, "DONT_LOG_TOOL_DATA", False)
        with caplog.at_level(logging.WARNING):
            with runtime.root_trace("saved"):
                with custom_span("authorization", data={"outcome": "success"}):
                    pass
            runtime.close()
        assert len(requests) == 1
        assert SENTINEL not in caplog.text
        assert all(record.message in SAFE_FAILURE_MESSAGES for record in caplog.records)
    finally:
        runtime.close()


def test_native_transport_request_error_is_single_attempt_and_nonfatal(
    monkeypatch, caplog
):
    attempts = []

    def fail_request(request):
        attempts.append(request)
        raise httpx2.ConnectError(SENTINEL, request=request)

    _, transport = serialized_requests(fail_request)
    native = make_native(transport)
    runtime = configure_tracing(settings(), native_exporter=native)
    try:
        import agents._debug as sdk_debug

        monkeypatch.setattr(sdk_debug, "DONT_LOG_MODEL_DATA", False)
        monkeypatch.setattr(sdk_debug, "DONT_LOG_TOOL_DATA", False)
        with caplog.at_level(logging.WARNING):
            with runtime.root_trace("saved"):
                with custom_span("authorization", data={"outcome": "success"}):
                    pass
            runtime.close()

        assert len(attempts) == 1
        assert SENTINEL not in caplog.text
        assert all(record.message in SAFE_FAILURE_MESSAGES for record in caplog.records)
    finally:
        runtime.close()


def test_flush_and_shutdown_failures_are_nonfatal_and_log_fixed_category(
    monkeypatch, caplog
):
    native = make_native()
    processor = SanitizedTraceProcessor(native)

    def fail(*_args, **_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(processor._batch, "_export_batches", fail)
    monkeypatch.setattr(processor._batch, "shutdown", fail)
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.trace_export"):
            processor.force_flush()
            processor.shutdown()
        assert SENTINEL not in caplog.text
        assert set(record.message for record in caplog.records) <= {
            "tracing_diagnostic_failure"
        }
    finally:
        native.close()


def test_worker_thread_failure_is_contained_without_a_traceback(monkeypatch, caplog):
    native = make_native()
    processor = SanitizedTraceProcessor(native)

    def fail(_self):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(BatchTraceProcessor, "_run", fail)
    monkeypatch.setattr(processor._batch, "shutdown", lambda **_kwargs: None)
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.trace_export"):
            processor.on_trace_start(PayloadItem(trace_payload()))
            worker = processor._batch._worker_thread
            assert worker is not None
            worker.join(timeout=1)
            assert not worker.is_alive()
            processor.shutdown()
        assert SENTINEL not in caplog.text
        assert [record.message for record in caplog.records] == [
            "tracing_diagnostic_failure"
        ]
    finally:
        native.close()


def test_replacing_runtime_discards_old_queue_and_ignores_late_callbacks():
    requests, transport = serialized_requests(
        lambda request: httpx2.Response(200, request=request)
    )
    old_native = make_native(transport)
    old_runtime = configure_tracing(settings(), native_exporter=old_native)
    try:
        old_processor = old_runtime._provider._multi_processor._processors[0]._processor
        old_processor._batch._ensure_thread_started = lambda: None
        with old_runtime.root_trace("saved"):
            with custom_span("authorization", data={"outcome": "success"}):
                pass
        assert old_processor._batch._queue.qsize() == 2

        replacement = configure_tracing(settings())
        try:
            assert old_runtime.enabled is False
            assert old_runtime.export_enabled is False
            assert old_processor._closed is True
            assert old_processor._batch._queue.empty()
            assert requests == []

            old_processor.on_trace_start(PayloadItem(trace_payload()))
            old_processor.on_span_end(PayloadItem(span_payload()))
            assert old_processor._batch._queue.empty()
            assert requests == []
        finally:
            replacement.close()
    finally:
        old_runtime.close()


def test_invalid_native_setup_is_nonfatal_and_disables_export(caplog):
    with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.tracing"):
        runtime = configure_tracing(settings(), native_exporter=object())
    try:
        assert runtime.enabled is True
        assert runtime.export_enabled is False
        assert [record.message for record in caplog.records] == [
            "tracing_diagnostic_failure"
        ]
    finally:
        runtime.close()


def test_invalid_application_authorization_error_still_propagates_with_native_exporter():
    requests, transport = serialized_requests(
        lambda request: httpx2.Response(200, request=request)
    )
    native = make_native(transport)
    runtime = configure_tracing(settings(), native_exporter=native)
    try:
        with pytest.raises(RuntimeError, match="authorization denied"):
            with runtime.root_trace("saved"):
                raise RuntimeError("authorization denied")
    finally:
        runtime.close()
    assert len(requests) == 1
