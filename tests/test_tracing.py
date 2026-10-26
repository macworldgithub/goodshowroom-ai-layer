"""Offline privacy contracts for native Agents SDK tracing."""

import logging
import re

import pytest
from agents.tracing import custom_span, get_trace_provider, trace
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.provider import DefaultTraceProvider

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.mongo import AuthorizationDenied
from goodshowroom_ai.tracing import configure_tracing

SAFE_FAILURE_MESSAGE = "tracing_diagnostic_failure"
SYNTHETIC_SECRET = "synthetic-secret-must-not-appear-in-logs"


class MemoryProcessor(TracingProcessor):
    """A real SDK processor that keeps synthetic trace objects in memory."""

    def __init__(self):
        self.trace_starts = []
        self.trace_ends = []
        self.span_starts = []
        self.span_ends = []
        self.flush_calls = 0
        self.shutdown_calls = 0

    def on_trace_start(self, trace):
        self.trace_starts.append(trace)

    def on_trace_end(self, trace):
        self.trace_ends.append(trace)

    def on_span_start(self, span):
        self.span_starts.append(span)

    def on_span_end(self, span):
        self.span_ends.append(span)

    def force_flush(self):
        self.flush_calls += 1

    def shutdown(self):
        self.shutdown_calls += 1


class FailingProcessor(TracingProcessor):
    """Raise one fixed synthetic error from every SDK callback."""

    def _fail(self):
        raise RuntimeError(SYNTHETIC_SECRET)

    def on_trace_start(self, trace):
        self._fail()

    def on_trace_end(self, trace):
        self._fail()

    def on_span_start(self, span):
        self._fail()

    def on_span_end(self, span):
        self._fail()

    def force_flush(self):
        self._fail()

    def shutdown(self):
        self._fail()


def settings(*, app_env="test", enabled=False, sensitive=False):
    return Settings(
        _env_file=None,
        app_env=app_env,
        openai_tracing_enabled=enabled,
        openai_tracing_include_sensitive_data=sensitive,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("development", "development"),
        ("test", "test"),
        ("production", "production"),
        ("staging", "unknown"),
        ("", "unknown"),
        ("Development", "unknown"),
        (None, "unknown"),
    ],
)
def test_settings_normalize_unknown_or_missing_app_environment(value, expected):
    assert settings(app_env=value).app_env == expected


def test_settings_default_environment_is_unknown_and_does_not_read_dotenv(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    assert Settings(_env_file=None).app_env == "unknown"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (" true ", True),
        ("TRUE", True),
        (False, False),
        ("false", False),
        ("yes", False),
        ("1", False),
        (1, False),
        (None, False),
    ],
)
def test_settings_accept_only_explicit_true_for_tracing(value, expected):
    assert settings(enabled=value).openai_tracing_enabled is expected


@pytest.mark.parametrize(
    ("app_env", "enabled_value", "normalized_env", "parsed_enabled", "expected"),
    [
        (None, "true", "unknown", True, False),
        ("development", None, "development", False, False),
        ("development", "true", "development", True, True),
        ("development", " TRUE ", "development", True, True),
        ("development", "false", "development", False, False),
        ("development", "yes", "development", False, False),
        ("development", "1", "development", False, False),
        ("development", "invalid", "development", False, False),
        ("test", "true", "test", True, False),
        ("production", "true", "production", True, False),
        ("staging", "true", "unknown", True, False),
    ],
)
def test_environment_matrix_enables_only_explicit_development_tracing(
    app_env, enabled_value, normalized_env, parsed_enabled, expected, monkeypatch
):
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    if enabled_value is None:
        monkeypatch.delenv("OPENAI_TRACING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OPENAI_TRACING_ENABLED", enabled_value)
    monkeypatch.setenv("OPENAI_TRACING_INCLUDE_SENSITIVE_DATA", "true")

    configuration = Settings(_env_file=None)
    assert configuration.app_env == normalized_env
    assert configuration.openai_tracing_enabled is parsed_enabled
    assert configuration.openai_tracing_include_sensitive_data is False
    runtime = configure_tracing(configuration)
    try:
        assert runtime.enabled is expected
        assert runtime.export_enabled is False
    finally:
        runtime.close()


def test_sensitive_capture_setting_is_forced_off():
    assert settings(sensitive=True).openai_tracing_include_sensitive_data is False


@pytest.mark.parametrize(
    ("app_env", "enabled", "expected"),
    [
        ("development", True, True),
        ("development", False, False),
        ("test", True, False),
        ("production", True, False),
        ("unknown", True, False),
    ],
)
def test_runtime_enables_only_explicit_development_metadata_tracing(
    app_env, enabled, expected
):
    capture = MemoryProcessor()
    runtime = configure_tracing(
        settings(app_env=app_env, enabled=enabled), capture_processor=capture
    )
    try:
        assert runtime.enabled is expected
        assert runtime.export_enabled is False
        assert isinstance(get_trace_provider(), DefaultTraceProvider)
        assert runtime.run_config("saved").tracing_disabled is (not expected)
    finally:
        runtime.close()


def test_no_default_exporter_is_constructed_during_configuration(monkeypatch):
    from agents.tracing import setup as sdk_setup
    from agents.tracing import processors as sdk_processors

    def forbidden_default_processor():
        raise AssertionError("SDK default processor must not be constructed")

    monkeypatch.setattr(sdk_setup, "GLOBAL_TRACE_PROVIDER", None)
    monkeypatch.setattr(
        sdk_processors, "default_processor", forbidden_default_processor
    )
    runtime = configure_tracing(
        settings(app_env="development", enabled=True),
        capture_processor=MemoryProcessor(),
    )
    try:
        assert runtime.enabled is True
        assert isinstance(get_trace_provider(), DefaultTraceProvider)
    finally:
        runtime.close()


def test_settings_representation_and_tracing_logs_do_not_expose_credentials(
    monkeypatch, caplog
):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("OPENAI_TRACING_ENABLED", "true")
    monkeypatch.setenv("OPENAI_TRACING_KEY", SYNTHETIC_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_SECRET)
    monkeypatch.setenv("AI_LAYER_CURSOR_ENCRYPTION_KEY", SYNTHETIC_SECRET)
    configuration = Settings(_env_file=None)

    with caplog.at_level(logging.DEBUG):
        runtime = configure_tracing(configuration)
        runtime.close()

    assert SYNTHETIC_SECRET not in repr(configuration)
    assert SYNTHETIC_SECRET not in caplog.text


def test_global_and_root_tracing_create_no_spans_when_disabled():
    capture = MemoryProcessor()
    runtime = configure_tracing(
        settings(app_env="test", enabled=True), capture_processor=capture
    )
    try:
        assert runtime.enabled is False
        config = runtime.run_config("saved")
        assert config.tracing_disabled is True
        assert config.trace_include_sensitive_data is False

        with trace(
            workflow_name="GoodShowroom.SavedChat",
            group_id="safe-global-group",
            metadata={"diagnostic_request_id": "safe-global-request"},
        ):
            with custom_span("synthetic-stage", data={"stage": "test"}):
                pass

        with runtime.root_trace("temporary"):
            with custom_span("synthetic-stage", data={"stage": "test"}):
                pass

        assert capture.trace_starts == []
        assert capture.trace_ends == []
        assert capture.span_starts == []
        assert capture.span_ends == []
    finally:
        runtime.close()


def test_run_configs_and_root_traces_use_safe_static_metadata():
    capture = MemoryProcessor()
    runtime = configure_tracing(
        settings(app_env="development", enabled=True), capture_processor=capture
    )
    try:
        saved = runtime.run_config("saved")
        temporary = runtime.run_config("temporary")
        for config, workflow in (
            (saved, "GoodShowroom.SavedChat"),
            (temporary, "GoodShowroom.TemporaryChat"),
        ):
            assert config.tracing_disabled is False
            assert config.workflow_name == workflow
            assert config.trace_include_sensitive_data is False
            assert config.model_settings.store is False
            assert set(config.trace_metadata) == {"diagnostic_request_id"}
            diagnostic_id = config.trace_metadata["diagnostic_request_id"]
            assert re.fullmatch(r"[0-9a-f]{32}", diagnostic_id)
            assert re.fullmatch(r"[0-9a-f]{32}", config.group_id)
        assert saved.group_id != temporary.group_id
        assert (
            saved.trace_metadata["diagnostic_request_id"]
            != temporary.trace_metadata["diagnostic_request_id"]
        )

        for mode, workflow in (
            ("saved", "GoodShowroom.SavedChat"),
            ("temporary", "GoodShowroom.TemporaryChat"),
        ):
            with runtime.root_trace(mode) as root:
                assert root.name == workflow
                assert re.fullmatch(r"[0-9a-f]{32}", root.group_id)
                assert set(root.metadata) == {"diagnostic_request_id"}
                assert re.fullmatch(
                    r"[0-9a-f]{32}", root.metadata["diagnostic_request_id"]
                )
                with custom_span("synthetic-stage", data={"stage": "authorization"}):
                    pass

        assert len(capture.trace_starts) == 2
        assert len(capture.trace_ends) == 2
        assert len(capture.span_starts) == 2
        assert len(capture.span_ends) == 2
        assert [item.name for item in capture.trace_ends] == [
            "GoodShowroom.SavedChat",
            "GoodShowroom.TemporaryChat",
        ]
    finally:
        runtime.close()


def test_replaced_runtime_cannot_create_spans_through_the_new_provider():
    old_capture = MemoryProcessor()
    new_capture = MemoryProcessor()
    old_runtime = configure_tracing(
        settings(app_env="development", enabled=True), capture_processor=old_capture
    )
    new_runtime = configure_tracing(
        settings(app_env="development", enabled=True), capture_processor=new_capture
    )
    try:
        assert old_runtime.enabled is False
        assert old_runtime.run_config("saved").tracing_disabled is True
        assert new_runtime.enabled is True

        with old_runtime.root_trace("saved"):
            with custom_span("synthetic-stage", data={"stage": "test"}):
                pass
        assert old_capture.trace_starts == []
        assert new_capture.trace_starts == []

        with new_runtime.root_trace("saved"):
            with custom_span("synthetic-stage", data={"stage": "test"}):
                pass
        assert len(new_capture.trace_starts) == 1
        assert len(new_capture.span_starts) == 1
    finally:
        old_runtime.close()
        new_runtime.close()


def test_invalid_mode_is_rejected_and_closed_runtime_configs_are_disabled():
    runtime = configure_tracing(settings(app_env="development", enabled=True))
    with pytest.raises(ValueError, match="Invalid tracing mode"):
        runtime.run_config("administrator")
    with pytest.raises(ValueError, match="Invalid tracing mode"):
        with runtime.root_trace("administrator"):
            pass

    runtime.close()
    assert runtime.enabled is False
    assert runtime.run_config("saved").tracing_disabled is True


@pytest.mark.parametrize("fault", ["construct", "start", "finish"])
def test_root_trace_contains_sdk_faults_and_logs_only_fixed_category(
    fault, monkeypatch, caplog
):
    from goodshowroom_ai import tracing as tracing_module

    runtime = configure_tracing(settings(app_env="development", enabled=True))
    events = []

    class FaultyTrace:
        def start(self, *, mark_as_current):
            events.append(("start", mark_as_current))
            if fault == "start":
                raise RuntimeError(SYNTHETIC_SECRET)

        def finish(self, *, reset_current):
            events.append(("finish", reset_current))
            if fault == "finish":
                raise RuntimeError(SYNTHETIC_SECRET)

    def factory(**_kwargs):
        if fault == "construct":
            raise RuntimeError(SYNTHETIC_SECRET)
        return FaultyTrace()

    monkeypatch.setattr(tracing_module, "trace", factory)
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.tracing"):
            with runtime.root_trace("saved") as root:
                if fault == "construct":
                    assert root is None
                else:
                    assert isinstance(root, FaultyTrace)

        assert SYNTHETIC_SECRET not in caplog.text
        assert [record.message for record in caplog.records] == [SAFE_FAILURE_MESSAGE]
        if fault == "construct":
            assert events == []
        elif fault == "start":
            assert events == [("start", True), ("finish", True)]
        else:
            assert events == [("start", True), ("finish", True)]
    finally:
        runtime.close()


def test_trace_processor_faults_are_nonfatal_and_do_not_log_exception_text(caplog):
    runtime = configure_tracing(
        settings(app_env="development", enabled=True),
        capture_processor=FailingProcessor(),
    )
    try:
        with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.tracing"):
            with runtime.root_trace("saved"):
                with custom_span("synthetic-stage", data={"stage": "test"}):
                    pass
            runtime.close()

        assert SYNTHETIC_SECRET not in caplog.text
        assert caplog.records
        assert {record.message for record in caplog.records} == {SAFE_FAILURE_MESSAGE}
    finally:
        runtime.close()


def test_runtime_close_contains_provider_flush_and_shutdown_faults(caplog):
    runtime = configure_tracing(settings(app_env="development", enabled=True))

    def fail_with_synthetic_secret():
        raise RuntimeError(SYNTHETIC_SECRET)

    runtime._provider.force_flush = fail_with_synthetic_secret
    runtime._provider.shutdown = fail_with_synthetic_secret
    with caplog.at_level(logging.WARNING, logger="goodshowroom_ai.tracing"):
        runtime.close()

    assert runtime.enabled is False
    assert SYNTHETIC_SECRET not in caplog.text
    assert [record.message for record in caplog.records] == [
        SAFE_FAILURE_MESSAGE,
        SAFE_FAILURE_MESSAGE,
    ]


def test_root_trace_does_not_suppress_authorization_errors():
    runtime = configure_tracing(settings(app_env="development", enabled=True))
    try:
        with pytest.raises(AuthorizationDenied):
            with runtime.root_trace("saved"):
                raise AuthorizationDenied("authorization denied")
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_app_configures_tracing_and_closes_it_during_lifespan_cleanup():
    application = create_app(settings(app_env="development", enabled=True))
    runtime = application.state.tracing
    assert runtime.enabled is True
    assert runtime.export_enabled is False
    assert not any("trace" in route.path.casefold() for route in application.routes)

    with pytest.raises(AuthorizationDenied):
        async with application.router.lifespan_context(application):
            assert runtime.enabled is True
            raise AuthorizationDenied("authorization denied")

    assert runtime.enabled is False
