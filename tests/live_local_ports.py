"""Reserve ports for the isolated Google test without starting a listener."""

import socket


class TestPortUnavailable(RuntimeError):
    """A local test port is busy or cannot be checked safely."""


def reserve_loopback(address: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        # On Windows, binding ::1 can succeed while a wildcard :: listener
        # already owns the same port. Reserve the wildcard IPv6 address so both
        # wildcard and loopback listeners cause a safe failure.
        bind_address = "::" if address == "::1" else address
        listener.bind((bind_address, port))
        return listener
    except OSError:
        listener.close()
        raise TestPortUnavailable(f"Local test port {port} is unavailable.") from None
