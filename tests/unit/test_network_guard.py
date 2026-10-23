"""The autouse network guard (tests/network_guard.py): a test can never reach a real provider by accident."""

import os
import socket

import httpx
import pytest

from tests import network_guard
from tests.network_guard import ExternalNetworkAccess, is_local_host


@pytest.mark.parametrize(
    "host", ["localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "::1", "[::1]", "::ffff:127.0.0.1"]
)
def test_loopback_names_and_addresses_are_local(host: str) -> None:
    assert is_local_host(host)


@pytest.mark.parametrize(
    "host",
    ["api.primitive.dev", "mcp.maritime.sh", "api.subq.ai", "10.0.0.1", "192.168.1.10", "0.0.0.0", "::", "", None, 7],
)
def test_everything_else_is_external(host: object) -> None:
    assert not is_local_host(host)


def test_external_connections_fail_loudly_and_are_recorded(network_guard_attempts: list[str]) -> None:
    with pytest.raises(ExternalNetworkAccess, match=r"api\.primitive\.dev:443"):
        socket.create_connection(("api.primitive.dev", 443), timeout=1)
    with socket.socket() as sock:
        with pytest.raises(ExternalNetworkAccess):
            sock.connect(("93.184.216.34", 443))
        with pytest.raises(ExternalNetworkAccess):
            sock.connect_ex(("mcp.maritime.sh", 443))
    with pytest.raises(ExternalNetworkAccess):
        socket.getaddrinfo("api.subq.ai", 443)
    assert network_guard_attempts == [
        "api.primitive.dev:443",
        "93.184.216.34:443",
        "mcp.maritime.sh:443",
        "api.subq.ai:443",
    ]
    network_guard_attempts.clear()  # this test provoked the guard on purpose; the teardown check must not fire


def test_a_real_http_client_cannot_reach_a_provider(network_guard_attempts: list[str]) -> None:
    with pytest.raises(httpx.HTTPError):  # httpx maps the OSError to ConnectError, so product code would swallow it
        httpx.get("https://api.primitive.dev/v1/threads/thr_1", timeout=2)
    assert network_guard_attempts  # ...which is why the attempt is recorded and the teardown fails the test
    network_guard_attempts.clear()


def test_loopback_connections_and_unix_sockets_are_not_blocked(tmp_path, network_guard_attempts: list[str]) -> None:  # noqa: ANN001
    with socket.socket() as sock:
        sock.settimeout(1)
        sock.connect_ex(("127.0.0.1", 1))  # whatever the OS answers, the guard let it through
    with socket.socket(socket.AF_UNIX) as unix_socket:
        with pytest.raises(OSError) as caught:  # nothing listens there
            unix_socket.connect(str(tmp_path / "nothing.sock"))
        assert not isinstance(caught.value, ExternalNetworkAccess)
    socket.getaddrinfo("localhost", 80)
    assert network_guard_attempts == []


def test_a_loopback_proxy_is_an_egress_path_and_counts_as_external(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in os.environ if n.lower().endswith("_proxy")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@localhost:59123")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.corp.example:3128")  # not loopback: blocked as external anyway
    assert network_guard.loopback_proxy_ports() == {59123}
    attempts = network_guard.install(monkeypatch)  # stacked on the autouse guard; its own record stays empty
    with pytest.raises(ExternalNetworkAccess):
        socket.create_connection(("localhost", 59123), timeout=1)
    with pytest.raises(ExternalNetworkAccess):
        socket.create_connection(("127.0.0.1", 59123), timeout=1)
    assert [a.split(" ")[0] for a in attempts] == ["localhost:59123", "127.0.0.1:59123"]
    assert all("egress proxy" in a for a in attempts)
    assert "secret" not in " ".join(attempts)
