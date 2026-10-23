"""Test-only deterministic controls and explicit database safety checks."""

import asyncio
import ipaddress
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID


class SafetyError(RuntimeError):
    """A test attempted an unapproved external operation."""


@dataclass
class Clock:
    value: datetime = datetime(2026, 10, 22, tzinfo=UTC)

    def now(self):
        return self.value

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("Clock must advance")
        self.value += timedelta(seconds=seconds)


class RaceGate:
    """Pause at a known point; tests release work without elapsed-time sleeps."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self):
        self.entered.set()
        await self.release.wait()


def blocked_network(*args, **kwargs):
    raise SafetyError("Network is disabled by the backend test harness")


def block_network(patch):
    # Keep socketpair available for the asyncio event loop, but deny IP traffic
    # and DNS. ASGITransport never opens a network socket.
    original_connect = socket.socket.connect
    original_pair = socket.socketpair
    internal = threading.local()

    def connect(sock, address):
        if (
            getattr(internal, "socketpair", False)
            and ipaddress.ip_address(address[0]).is_loopback
        ):
            return original_connect(sock, address)
        return blocked_network()

    def socketpair(*args, **kwargs):
        # Windows implements socketpair with a temporary self-connected TCP pair.
        # Only this synchronous standard-library call can use that loopback path.
        internal.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            internal.socketpair = False

    patch.setattr(socket.socket, "connect", connect)
    patch.setattr(socket, "socketpair", socketpair)
    for name in ("connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            patch.setattr(socket.socket, name, blocked_network)
    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        patch.setattr(socket, name, blocked_network)


@dataclass(frozen=True)
class DatabaseTarget:
    """No URI, client, mutation, or cleanup capability is exposed here."""

    host: str
    port: int
    run_id: UUID
    databases: tuple[str, ...]


def verify_database_target(
    *, host, port, run_id, databases, approved_endpoint, read_marker
):
    """Require reviewed endpoint AND per-database ownership evidence.

    A future adapter must supply an actual read-only marker reader. Synthetic
    readers in unit tests prove this guard's logic, not a real Mongo server.
    Cleanup callers must re-run this check immediately before cleanup.
    """
    try:
        address = ipaddress.ip_address(host)
        identifier = UUID(run_id)
    except (ValueError, TypeError, AttributeError):
        raise SafetyError("Invalid test database target") from None
    if (
        not address.is_loopback
        or type(port) is not int
        or not 1 <= port <= 65535
        or (str(address), port) != approved_endpoint
    ):
        raise SafetyError("Test endpoint has not been approved")
    expected = tuple(
        f"gsr_test_{identifier.hex}_{part}" for part in ("leads", "panel", "app")
    )
    if tuple(databases) != expected:
        raise SafetyError("Database namespace is not owned by this test run")
    for name in expected:
        try:
            marker = read_marker(str(address), port, name)
        except Exception:
            raise SafetyError("Test database ownership cannot be verified") from None
        if marker != {"run_id": str(identifier), "purpose": "isolated-fixtures"}:
            raise SafetyError("Test database ownership cannot be verified")
    return DatabaseTarget(str(address), port, identifier, expected)
