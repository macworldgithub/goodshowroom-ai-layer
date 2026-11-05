"""The live Google test must not share another service's callback port."""

import socket

import pytest

from tests.live_local_ports import TestPortUnavailable as LivePortError
from tests.live_local_ports import reserve_loopback


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_occupied_loopback_port_is_rejected(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    if family == socket.AF_INET6 and not socket.has_ipv6:
        pytest.skip("IPv6 is not available")
    with socket.socket(family, socket.SOCK_STREAM) as existing:
        existing.bind((address, 0))
        existing.listen(1)
        port = existing.getsockname()[1]
        with pytest.raises(LivePortError, match="Local test port"):
            reserve_loopback(address, port)


def test_available_loopback_port_can_be_reserved():
    with reserve_loopback("127.0.0.1", 0) as listener:
        assert listener.getsockname()[0] == "127.0.0.1"
        assert listener.getsockname()[1] > 0


def test_ipv6_wildcard_listener_blocks_localhost_callback():
    if not socket.has_ipv6:
        pytest.skip("IPv6 is not available")
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as existing:
        existing.bind(("::", 0))
        existing.listen(1)
        with pytest.raises(LivePortError):
            with reserve_loopback("::1", existing.getsockname()[1]):
                pass


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0"])
def test_ipv4_listeners_block_wildcard_preflight(address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
        existing.bind((address, 0))
        existing.listen(1)
        with pytest.raises(LivePortError):
            with reserve_loopback("0.0.0.0", existing.getsockname()[1]):
                pass
