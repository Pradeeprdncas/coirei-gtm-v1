"""Outbound connection guard (SSRF protection).

Target URLs come from untrusted pages, so every TCP connection the crawler
opens goes through ``NetGuard``: the host is resolved once, every resolved
address must be a globally routable unicast address, and the connection is
made to that already-checked address. Connecting to the checked IP (rather
than handing the hostname back to the OS) closes the DNS-rebinding window
between check and use. TLS still verifies the certificate against the
original hostname because the HTTP layer negotiates TLS after
``connect_tcp`` using the request's host.

The guard sits at the connection layer, so it covers every redirect hop and
every request made through ``guarded_transport``, not just the first URL.

* Resolutions are cached (bounded size and age). The policy is applied to
  cached addresses on every use, so a policy change applies at once, and the
  connection still goes to a checked address.
* ``ip_policy(allowed_networks)`` lets a deployment explicitly allow networks
  that are otherwise refused (spec section 9: "unless explicitly allowed by
  deployment policy"), for example an internal test site. Cloud metadata
  endpoints are refused even then.
* A name that does not resolve raises ``DnsResolutionError``: a network
  failure (retryable), not a policy refusal.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpcore
import httpx

log = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
# Not globally routable, yet ``is_global`` (Python 3.11) calls them global:
# IPv4-compatible (deprecated), SIIT IPv4-translated, local-use NAT64
# (RFC 8215), site-local (deprecated). Translators on the path could reach
# private IPv4 through them, so they are refused outright.
_NOT_GLOBAL_V6 = tuple(
    ipaddress.IPv6Network(n) for n in ("::/96", "::ffff:0:0:0/96", "64:ff9b:1::/48", "fec0::/10")
)
# Instance metadata services (AWS/GCP/Azure/OCI, AWS IPv6, Alibaba). Never
# reachable, whatever the deployment allows: they hand out credentials.
METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "169.254.170.2")
)


class NetGuardError(Exception):
    """A connection was refused by policy. Never retried."""


class DnsResolutionError(Exception):
    """The host name could not be resolved (a network failure, may be retried)."""


def ip_policy(allowed_networks: Iterable[str] = ()) -> Callable[[IPAddress], bool]:
    """Public addresses, plus explicitly allowed networks; never metadata endpoints."""
    networks = [ipaddress.ip_network(n, strict=False) for n in allowed_networks]

    def allowed(ip: IPAddress) -> bool:
        # Also in IPv4-mapped/6to4/NAT64/Teredo form, whatever networks are allowed.
        if any(a in METADATA_ADDRESSES for a in (ip, *_embedded_ipv4(ip))):
            return False
        return is_public_ip(ip) or any(ip in net for net in networks)

    return allowed


def _embedded_ipv4(ip: IPAddress) -> list[ipaddress.IPv4Address]:
    """The IPv4 addresses an IPv6 address carries (mapped, 6to4, NAT64, Teredo)."""
    if isinstance(ip, ipaddress.IPv4Address):
        return []
    embedded = ip.ipv4_mapped or ip.sixtofour
    if embedded is None and ip in _NAT64:
        embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if embedded is not None:
        return [embedded]
    return list(ip.teredo or ())


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses.

    ``is_global`` excludes private, loopback, link-local (including the
    169.254.169.254 cloud metadata endpoint), shared/CGNAT, documentation,
    benchmarking and reserved ranges. IPv6 forms that embed an IPv4 address
    are judged by the embedded address.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in net for net in _NOT_GLOBAL_V6):
            return False
        embedded = _embedded_ipv4(ip)
        if embedded:
            return all(is_public_ip(part) for part in embedded)
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class NetGuard:
    def __init__(
        self,
        allowed_ports: Iterable[int],
        resolver: Resolver = system_resolver,
        ip_allowed: Callable[[IPAddress], bool] | None = None,
        cache_size: int = 1024,
        cache_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.allowed_ports = frozenset(allowed_ports)
        self._resolver = resolver
        self._ip_allowed = ip_allowed or ip_policy()
        self._cache: OrderedDict[tuple[str, int], tuple[float, list[str]]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_seconds = cache_seconds
        self._clock = clock

    async def _lookup(self, host: str, port: int) -> list[str]:
        key = (host.lower(), port)
        hit = self._cache.get(key)
        if hit is not None and hit[0] > self._clock():
            self._cache.move_to_end(key)
            return hit[1]
        try:
            resolved = await self._resolver(host, port)
        except (OSError, UnicodeError) as exc:
            raise DnsResolutionError(f"cannot resolve {host}: {exc}") from exc
        if self._cache_size:
            self._cache[key] = (self._clock() + self._cache_seconds, resolved)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return resolved

    async def resolve(self, host: str, port: int) -> list[str]:
        """Return the addresses to connect to, or raise if any is disallowed.

        Fails closed: a host with even one disallowed address is refused, since
        which address the connection would use is not under our control.
        """
        if port not in self.allowed_ports:
            raise NetGuardError(f"port {port} not allowed")
        host = host.strip("[]")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            resolved = await self._lookup(host, port)
            addresses = [ipaddress.ip_address(a.split("%", 1)[0]) for a in resolved]
        if not addresses:
            raise NetGuardError(f"no addresses for {host}")
        blocked = [str(a) for a in addresses if not self._ip_allowed(a)]
        if blocked:
            log.warning("netguard.blocked", extra={"host": host, "addresses": blocked})
            raise NetGuardError(f"{host} resolves to non-public address {blocked[0]}")
        return list(dict.fromkeys(str(a) for a in addresses))


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, guard: NetGuard) -> None:
        self._guard = guard
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        last_error: Exception | None = None
        for address in await self._guard.resolve(host, port):
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise NetGuardError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(
    guard: NetGuard, http2: bool = False, verify: ssl.SSLContext | bool = True
) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose every TCP connection passes through ``guard``.

    httpx has no public hook for the network backend, so the pool's backend is
    replaced directly. httpx is pinned to 0.28.x in pyproject, and
    tests/test_netguard.py fails if the guard is ever bypassed. ``http2``
    negotiates HTTP/2 by ALPN where the server offers it (needs ``h2``).
    ``verify`` is for tests with their own certificate authority; certificates
    are always verified.
    """
    if verify is False:
        raise ValueError("certificate verification cannot be turned off")
    transport = httpx.AsyncHTTPTransport(retries=0, http2=http2, verify=verify)
    pool = transport._pool
    if not hasattr(pool, "_network_backend"):
        raise RuntimeError("unsupported httpcore version: cannot install network guard")
    pool._network_backend = _GuardedBackend(guard)
    return transport
