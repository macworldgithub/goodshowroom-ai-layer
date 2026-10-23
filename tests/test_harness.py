"""Harness contracts; no real provider or MongoDB evidence is claimed."""

import asyncio
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from goodshowroom_ai.config import Settings
from tests.harness import SafetyError, verify_database_target


@pytest.mark.parametrize(
    "host", ["accounts.google.com", "api.openai.com", "127.0.0.1", "::1"]
)
def test_network_is_denied(host):
    with pytest.raises(SafetyError, match="Network is disabled"):
        socket.create_connection((host, 443))


def test_dns_and_udp_are_denied():
    with pytest.raises(SafetyError):
        socket.getaddrinfo("example.invalid", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(SafetyError):
            udp.sendto(b"test", ("127.0.0.1", 9))


def test_real_configuration_is_unavailable():
    assert Settings().app_env == "test"
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["OPENAI_TRACING_ENABLED"] == "false"
    root_env = Path(__file__).resolve().parents[1] / ".env"
    for mode in ("r", "r+", "w"):
        with pytest.raises(SafetyError, match="Environment files"):
            root_env.open(mode)
    with pytest.raises(SafetyError):
        root_env.with_name(".ENV").read_text()
    with pytest.raises(SafetyError):
        root_env.with_name(".ENV").read_text()
    with pytest.raises(SafetyError):
        os.open(root_env, os.O_RDONLY)
    with pytest.raises(SafetyError):
        Settings(_env_file=root_env)


@pytest.mark.parametrize("repetition", range(2))
@pytest.mark.asyncio
async def test_fixtures_start_clean(
    repetition, clock, model, policy, repository, exporter, monkeypatch
):
    assert clock.now().year == 2026
    assert clock.now().second == 0
    assert "HARNESS_ISOLATION_MARKER" not in os.environ
    assert "HARNESS_DIRECT_MARKER" not in os.environ
    assert (
        not model.called
        and not policy.called
        and not repository.called
        and not exporter.called
    )
    assert await policy() is False
    with pytest.raises(RuntimeError, match="No scripted model"):
        await model()
    with pytest.raises(RuntimeError, match="No scripted repository"):
        await repository()
    monkeypatch.setenv("HARNESS_ISOLATION_MARKER", str(repetition))
    os.environ["HARNESS_DIRECT_MARKER"] = str(repetition)
    clock.advance(10)


@pytest.mark.asyncio
async def test_scripted_outcomes_and_cancellation(model, policy, repository, exporter):
    model.side_effect = None
    model.return_value = {"status": "synthetic"}
    policy.return_value = True
    repository.side_effect = None
    repository.return_value = []
    assert await policy() is True
    assert await model() == {"status": "synthetic"}
    assert await repository() == []
    await exporter({"stage": "test"})
    exporter.assert_awaited_once()
    model.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await model()


@pytest.mark.asyncio
async def test_clock_and_race_control(clock, race_gate):
    task = asyncio.create_task(race_gate.wait())
    try:
        await asyncio.wait_for(race_gate.entered.wait(), timeout=2)
        assert not task.done()
        clock.advance(60)
        assert clock.now().minute == 1
        race_gate.release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("repetition", range(2))
def test_runtime_signing_keys(signing_key, repetition):
    payload = f"synthetic-{repetition}".encode()
    signature = signing_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    assert signing_key.key_size == 3072
    signing_key.public_key().verify(
        signature, payload, padding.PKCS1v15(), hashes.SHA256()
    )
    with pytest.raises(InvalidSignature):
        signing_key.public_key().verify(
            signature, b"changed", padding.PKCS1v15(), hashes.SHA256()
        )


@pytest.mark.asyncio
async def test_lifespan_and_override_cleanup_on_failure(app, app_client):
    events = []

    @asynccontextmanager
    async def lifespan(application):
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    app.router.lifespan_context = lifespan
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        async with app_client() as client:
            app.dependency_overrides[object()] = lambda: None
            assert (await client.get("/v1/chats")).status_code == 404
            raise RuntimeError("synthetic interruption")
    assert events == ["start", "stop"]
    assert app.dependency_overrides == {}
    assert app.state._state == {}


@pytest.mark.asyncio
async def test_app_fixture_is_fresh(app, app_client):
    assert app.dependency_overrides == {}
    assert app.state.environment == "test"
    async with app_client() as client:
        assert (await client.get("/docs")).status_code == 404


def test_default_pytest_temp_root_is_unique_and_writable(tmp_path, pytestconfig):
    base_temp = pytestconfig.option.basetemp
    assert base_temp is not None
    base_temp = Path(base_temp).resolve()

    explicit_basetemp = any(
        argument == "--basetemp" or argument.startswith("--basetemp=")
        for argument in pytestconfig.invocation_params.args
    )
    if not explicit_basetemp:
        assert base_temp.name == "pytest"
        assert base_temp.parent.name.startswith("goodshowroom-pytest-")

    resolved_tmp_path = tmp_path.resolve()
    assert resolved_tmp_path.is_relative_to(base_temp)
    marker = resolved_tmp_path / "writable-marker.txt"
    marker.write_text("synthetic", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "synthetic"


def test_default_pytest_config_disables_cacheprovider(pytestconfig):
    addopts = pytestconfig.getini("addopts")
    assert "-p" in addopts
    assert "no:cacheprovider" in addopts
    assert not pytestconfig.pluginmanager.hasplugin("cacheprovider")


def target_arguments():
    run_id = uuid4()
    return dict(
        host="127.0.0.1",
        port=27018,
        run_id=str(run_id),
        databases=tuple(
            f"gsr_test_{run_id.hex}_{part}" for part in ("leads", "panel", "app")
        ),
        approved_endpoint=("127.0.0.1", 27018),
        read_marker=lambda *args: {
            "run_id": str(run_id),
            "purpose": "isolated-fixtures",
        },
    )


def test_verified_synthetic_target_is_accepted():
    arguments = target_arguments()
    target = verify_database_target(**arguments)
    assert target.databases == arguments["databases"]
    assert not hasattr(target, "drop_database")


@pytest.mark.parametrize(
    "change",
    [
        {"databases": ("byd-leads-new", "byd-panel", "byd_ai_layer")},
        {"databases": ("leads_test", "panel_test", "app_test")},
        {"host": "atlas.example.invalid"},
        {"host": "192.0.2.1"},
        {"port": 27017},
        {"run_id": "invalid"},
        {"read_marker": lambda *args: None},
        {
            "read_marker": lambda *args: {
                "purpose": "isolated-fixtures",
                "run_id": str(uuid4()),
            }
        },
    ],
)
def test_unsafe_or_unowned_database_target_is_rejected(change):
    with pytest.raises(SafetyError):
        verify_database_target(**(target_arguments() | change))


def test_failed_marker_read_denies_target():
    def unavailable(*args):
        raise RuntimeError("Synthetic marker failure")

    with pytest.raises(SafetyError, match="cannot be verified"):
        verify_database_target(**(target_arguments() | {"read_marker": unavailable}))


@pytest.mark.parametrize(
    "marker,option,expected",
    [
        ("integration", None, "require --allow-test-db"),
        ("integration", "--allow-test-db", "No verified real database adapter"),
        ("live", None, "require --allow-live-model"),
        ("live", "--allow-live-model", "No reviewed live model adapter"),
        ("unknown_marker", None, "not found in `markers`"),
    ],
)
def test_marker_gates_in_separate_pytest_process(tmp_path, marker, option, expected):
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        f"import pytest\n@pytest.mark.{marker}\ndef test_probe():\n raise AssertionError('BODY_EXECUTED')\n"
    )
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(root / "pyproject.toml"),
        "-p",
        "tests.conftest",
        "--rootdir",
        str(tmp_path),
        "--confcutdir",
        str(tmp_path),
        str(probe),
        "-q",
        "-m",
        marker,
    ]
    if option:
        command.append(option)
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=30
    )
    output = result.stdout + result.stderr
    assert result.returncode in (2, 4)
    assert expected in output
    assert "BODY_EXECUTED" not in output


def test_network_guard_is_active_during_collection(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import socket\nsocket.create_connection(('127.0.0.1', 9))\ndef test_probe(): pass\n"
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(root / "pyproject.toml"),
            "-p",
            "tests.conftest",
            "--rootdir",
            str(tmp_path),
            "--confcutdir",
            str(tmp_path),
            str(probe),
            "-q",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "Network is disabled" in result.stdout + result.stderr


def test_session_guards_restore_after_pytest_exit(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import os\n"
        "def test_probe():\n"
        " assert 'HARNESS_SESSION_MARKER' not in os.environ\n"
        " assert os.environ['USERNAME'] == 'Synthetic USERNAME'\n"
        " assert os.environ['USER'] == 'Synthetic USER'\n"
        " assert os.environ['LOGNAME'] == 'Synthetic LOGNAME'\n"
        " assert os.environ['LNAME'] == 'Synthetic LNAME'\n"
    )
    root = Path(__file__).resolve().parents[1]
    driver = """
import builtins, io, os, socket, sys
from uuid import uuid4
import pytest
from goodshowroom_ai.config import Settings
marker = str(uuid4())
os.environ['HARNESS_SESSION_MARKER'] = marker
user_names = {
    'USERNAME': 'Synthetic USERNAME',
    'USER': 'Synthetic USER',
    'LOGNAME': 'Synthetic LOGNAME',
    'LNAME': 'Synthetic LNAME',
}
os.environ.update(user_names)
before = (builtins.open, io.open, os.open, socket.socket.connect, socket.socketpair)
configuration = dict(Settings.model_config)
assert pytest.main(sys.argv[1:]) == 0
assert before == (builtins.open, io.open, os.open, socket.socket.connect, socket.socketpair)
assert Settings.model_config == configuration
assert os.environ['HARNESS_SESSION_MARKER'] == marker
assert {name: os.environ[name] for name in user_names} == user_names
print('SESSION_RESTORED')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            driver,
            "-c",
            str(root / "pyproject.toml"),
            "-p",
            "tests.conftest",
            "--rootdir",
            str(tmp_path),
            "--confcutdir",
            str(tmp_path),
            str(probe),
            "-q",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SESSION_RESTORED" in result.stdout
