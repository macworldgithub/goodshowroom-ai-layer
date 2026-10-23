"""Install isolation before test-module collection; provide fresh dependencies."""

import builtins
import io
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from goodshowroom_ai.app import create_app
from goodshowroom_ai.config import Settings
from tests.harness import Clock, RaceGate, SafetyError, block_network


def pytest_addoption(parser):
    parser.addoption("--allow-test-db", action="store_true", default=False)
    parser.addoption("--allow-live-model", action="store_true", default=False)


def pytest_configure(config):
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    if config.option.basetemp is None:
        temporary_root = tempfile.TemporaryDirectory(prefix="goodshowroom-pytest-")
        config.option.basetemp = str(Path(temporary_root.name) / "pytest")
        config.add_cleanup(temporary_root.cleanup)
    keep = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "COMSPEC",
        "PATHEXT",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "HOME",
        "USERNAME",
        "USER",
        "LOGNAME",
        "LNAME",
    }
    for name in list(os.environ):
        if name.upper() not in keep:
            patch.delenv(name)
    patch.setenv("APP_ENV", "test")
    patch.setenv("OPENAI_TRACING_ENABLED", "false")
    patch.setenv("OPENAI_TRACING_INCLUDE_SENSITIVE_DATA", "false")
    patch.setitem(Settings.model_config, "env_file", None)

    def safe_open(original):
        def guarded(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                name = Path(os.fsdecode(file)).name.casefold()
                if name == ".env" or name.startswith(".env."):
                    raise SafetyError("Environment files are disabled during tests")
            return original(file, mode, *args, **kwargs)

        return guarded

    patch.setattr(builtins, "open", safe_open(builtins.open))
    patch.setattr(io, "open", safe_open(io.open))
    original_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(path)).name.casefold()
            if name == ".env" or name.startswith(".env."):
                raise SafetyError("Environment files are disabled during tests")
        return original_os_open(path, flags, *args, **kwargs)

    patch.setattr(os, "open", guarded_os_open)
    block_network(patch)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("integration"):
            if not config.getoption("--allow-test-db"):
                raise pytest.UsageError(
                    "Integration tests require --allow-test-db and a verified target"
                )
            raise pytest.UsageError(
                "No verified real database adapter is configured yet"
            )
        if item.get_closest_marker("live"):
            if not config.getoption("--allow-live-model"):
                raise pytest.UsageError(
                    "Live tests require --allow-live-model and reviewed synthetic inputs"
                )
            raise pytest.UsageError("No reviewed live model adapter is configured yet")


@pytest.fixture(autouse=True)
def fresh_environment():
    before = dict(os.environ)
    os.environ["APP_ENV"] = "test"
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture
def test_settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def race_gate():
    return RaceGate()


@pytest.fixture
def model():
    return AsyncMock(side_effect=RuntimeError("No scripted model outcome"))


@pytest.fixture
def policy():
    return AsyncMock(return_value=False)


@pytest.fixture
def repository():
    return AsyncMock(side_effect=RuntimeError("No scripted repository outcome"))


@pytest.fixture
def exporter():
    return AsyncMock(return_value=None)


@pytest.fixture
def signing_key():
    # Never serialize or persist the private key.
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture
def app(test_settings):
    application = create_app(test_settings)
    yield application
    application.dependency_overrides.clear()
    application.state._state.clear()


@pytest.fixture
def app_client(app):
    @asynccontextmanager
    async def open_client():
        try:
            async with app.router.lifespan_context(app):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test.invalid"
                ) as client:
                    yield client
        finally:
            app.dependency_overrides.clear()
            app.state._state.clear()

    return open_client
