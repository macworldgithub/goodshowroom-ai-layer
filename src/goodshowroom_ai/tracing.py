"""Explicit SDK privacy controls and opt-in sanitized native export.

This module configures the SDK before use; startup supplies no native exporter
until provider access and retention are reviewed.
The optional processor seam is for trusted, in-memory diagnostic tests only.
An application role does not grant OpenAI dashboard access.
"""

import logging
from contextlib import contextmanager
from typing import Literal
from uuid import uuid4

from agents import ModelSettings, RunConfig, set_trace_processors, set_tracing_disabled
from agents.tracing import get_trace_provider, set_trace_provider, trace
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.processors import BackendSpanExporter

from goodshowroom_ai.config import Settings
from goodshowroom_ai.trace_export import SanitizedTraceProcessor, protect_sdk_logs

ChatMode = Literal["saved", "temporary"]
_WORKFLOWS = {
    "saved": "GoodShowroom.SavedChat",
    "temporary": "GoodShowroom.TemporaryChat",
}
_logger = logging.getLogger(__name__)


def _workflow(mode: ChatMode) -> str:
    if not isinstance(mode, str) or mode not in _WORKFLOWS:
        raise ValueError("Invalid tracing mode")
    return _WORKFLOWS[mode]


class _SafeCaptureProcessor(TracingProcessor):
    """Contain diagnostic failures before SDK logging can include exceptions."""

    def __init__(self, processor: TracingProcessor):
        self._processor = processor

    def _call(self, operation: str, *args) -> None:
        try:
            getattr(self._processor, operation)(*args)
        except Exception:
            # Fixed category only: no exception, payload, IDs, keys or traceback.
            _logger.warning("tracing_diagnostic_failure")

    def on_trace_start(self, trace):
        self._call("on_trace_start", trace)

    def on_trace_end(self, trace):
        self._call("on_trace_end", trace)

    def on_span_start(self, span):
        self._call("on_span_start", span)

    def on_span_end(self, span):
        self._call("on_span_end", span)

    def force_flush(self):
        self._call("force_flush")

    def shutdown(self):
        self._call("shutdown")


class TracingRuntime:
    """One process tracing policy; creates no business or identity permissions."""

    def __init__(
        self,
        provider: DefaultTraceProvider,
        enabled: bool,
        *,
        export_enabled: bool = False,
    ):
        self._provider = provider
        self._enabled = enabled
        self._closed = False
        self._export_enabled = export_enabled

    @property
    def enabled(self) -> bool:
        return (
            self._enabled
            and not self._closed
            and get_trace_provider() is self._provider
        )

    @property
    def export_enabled(self) -> bool:
        return self.enabled and self._export_enabled

    def run_config(self, mode: ChatMode) -> RunConfig:
        """Fresh diagnostic IDs cannot link traces to source or login records.

        store=False controls model response storage independently of tracing.
        TICKET-009 must use this config for every foreground agent invocation.
        """
        return RunConfig(
            tracing_disabled=not self.enabled,
            trace_include_sensitive_data=False,
            workflow_name=_workflow(mode),
            group_id=uuid4().hex,
            trace_metadata={"diagnostic_request_id": uuid4().hex},
            model_settings=ModelSettings(store=False),
        )

    @contextmanager
    def root_trace(self, mode: ChatMode):
        """Configure the outer SDK trace without swallowing application errors."""
        config = self.run_config(mode)
        root = None
        try:
            root = trace(
                workflow_name=config.workflow_name,
                group_id=config.group_id,
                metadata=config.trace_metadata,
                disabled=config.tracing_disabled,
            )
            root.start(mark_as_current=True)
        except Exception:
            _logger.warning("tracing_diagnostic_failure")
        try:
            yield root
        finally:
            if root is not None:
                try:
                    root.finish(reset_current=True)
                except Exception:
                    _logger.warning("tracing_diagnostic_failure")

    def close(self, *, discard: bool = False) -> None:
        """Stop diagnostics; dropped telemetry must not break normal cleanup."""
        if self._closed:
            return
        self._closed = True
        self._provider.set_disabled(True)
        operations = (
            (self._provider.shutdown,)
            if discard
            else (self._provider.force_flush, self._provider.shutdown)
        )
        for operation in operations:
            try:
                operation()
            except Exception:
                _logger.warning("tracing_diagnostic_failure")


_active_runtime: TracingRuntime | None = None


def configure_tracing(
    settings: Settings,
    *,
    capture_processor: TracingProcessor | None = None,
    native_exporter: BackendSpanExporter | None = None,
) -> TracingRuntime:
    """Replace defaults before any trace; app startup supplies no processor.

    native_exporter is a trusted startup seam, never an HTTP/admin setting.
    It must be supplied only after provider access/retention review. The normal
    app factory supplies none. Tests inject the exact SDK exporter with an
    in-memory HTTP transport. No per-trace credentials or destination overrides.

    Importing the pinned SDK is lazy with respect to its global trace provider.
    Install a fresh provider first so default_processor()/BackendSpanExporter
    are never constructed. Use the supported global setters after replacement.
    Reconfigure only at startup, never per request. Existing runtimes deny use
    after another runtime replaces their provider.
    """
    if capture_processor is not None and native_exporter is not None:
        raise ValueError("Multiple trace destinations are not permitted")
    global _active_runtime
    if _active_runtime is not None:
        # A previous processor must not keep exporting in parallel with the
        # replacement. Drop queued diagnostics instead of flushing on disable.
        _active_runtime.close(discard=True)
    protect_sdk_logs()
    provider = DefaultTraceProvider()
    provider.set_disabled(True)
    set_trace_provider(provider)
    enabled = (
        settings.app_env == "development" and settings.openai_tracing_enabled is True
    )
    processors = (
        [_SafeCaptureProcessor(capture_processor)]
        if enabled and capture_processor is not None
        else []
    )
    export_enabled = False
    if enabled and native_exporter is not None:
        try:
            processors = [
                _SafeCaptureProcessor(SanitizedTraceProcessor(native_exporter))
            ]
            export_enabled = True
        except Exception:
            _logger.warning("tracing_diagnostic_failure")
    set_trace_processors(processors)
    set_tracing_disabled(not enabled)
    runtime = TracingRuntime(provider, enabled, export_enabled=export_enabled)
    _active_runtime = runtime
    return runtime
