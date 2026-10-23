"""Make any attempt to reach a non-local host fail loudly, in every test.

Provider HTTP is faked with `httpx.MockTransport` / in-process fakes (spec section 17); a test that reaches a real
provider is a bug, and the sandbox that used to stop it is not part of the suite. `install` patches the Python socket
entry points (`socket.socket.connect` / `connect_ex`, `socket.create_connection`, `socket.getaddrinfo`):

- loopback (127.0.0.0/8, ::1, `localhost`) and unix sockets are allowed, EXCEPT the port of any configured HTTP(S)
  proxy that lives on loopback: a sandbox or corporate egress proxy listens on localhost, so a connection to it is
  really a connection to whatever host the request names (httpx/httpx2 read the proxy from the environment);
- anything else raises `ExternalNetworkAccess` at the call site AND is recorded, so the autouse fixture in conftest can
  fail the test at teardown even when product code swallowed the error (the provider clients turn every transport
  error into `ProviderUnavailable`, which would otherwise let a test pass by accident).

Not covered: C-level connections that never touch Python's `socket` module (libpq, i.e. psycopg to a Postgres at
TEST_DATABASE_URL). Those are the test database, not a provider.
"""

import ipaddress
import socket
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import pytest

LOCAL_NAMES = frozenset({"localhost"})
# AF_INET / AF_INET6 are the only families that leave the machine; AF_UNIX and friends never do.
_NETWORK_FAMILIES = (socket.AF_INET, socket.AF_INET6)
_DEFAULT_PROXY_PORTS = {"http": 80, "https": 443, "socks4": 1080, "socks5": 1080, "socks5h": 1080}


class ExternalNetworkAccess(OSError):
    """A test (or the code under test) tried to open a connection to a non-local host."""


def is_local_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").split("%", 1)[0].lower()  # drop IPv6 brackets and a scope id
    if name in LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def loopback_proxy_ports() -> frozenset[int]:
    """Ports of the configured proxies (environment, or the macOS system settings) that listen on loopback."""
    ports: set[int] = set()
    for scheme, value in urllib.request.getproxies().items():
        if scheme == "no" or not isinstance(value, str) or not value:
            continue
        try:
            parts = urlsplit(value if "://" in value else f"//{value}")
            host, port = parts.hostname, parts.port or _DEFAULT_PROXY_PORTS.get(parts.scheme, 80)
        except ValueError:
            continue
        if host is not None and is_local_host(host):
            ports.add(port)
    return frozenset(ports)


def _describe(address: Any, proxy_ports: frozenset[int] = frozenset()) -> str:
    if isinstance(address, tuple) and address:
        host = address[0]
        port = address[1] if len(address) > 1 else "?"
        if is_local_host(host) and port in proxy_ports:
            return f"{host}:{port} (the configured egress proxy, so a request to some external host)"
        return f"{host}:{port}"
    return repr(address)


def install(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the socket entry points for the current test. Returns the list of blocked destinations (usually empty)."""
    attempts: list[str] = []
    proxy_ports = loopback_proxy_ports()

    def allowed(address: Any) -> bool:
        if not (isinstance(address, tuple) and len(address) >= 2 and is_local_host(address[0])):
            return False
        return address[1] not in proxy_ports

    def refuse(destination: str) -> ExternalNetworkAccess:
        attempts.append(destination)
        return ExternalNetworkAccess(
            f"blocked: a test tried to reach {destination}. Use a fake transport (httpx.MockTransport) or an "
            "in-process fake instead of the network."
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def check_socket(sock: socket.socket, address: Any) -> None:
        if sock.family in _NETWORK_FAMILIES and not allowed(address):
            raise refuse(_describe(address, proxy_ports))

    def connect(self: socket.socket, address: Any) -> None:
        check_socket(self, address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check_socket(self, address)
        return real_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if not allowed(address):
            raise refuse(_describe(address, proxy_ports))
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not is_local_host(host):  # None is a passive (wildcard) lookup
            raise refuse(_describe((host, port)))
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts
