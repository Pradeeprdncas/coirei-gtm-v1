import asyncio
import ipaddress
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

from gtm_crawler.config import FetchSettings, UrlSettings
from gtm_crawler.fetch.http import FetchError, HttpFetcher
from gtm_crawler.netguard import DnsResolutionError, NetGuard, NetGuardError, is_public_ip

from .helpers import IDENTITY


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),  # noqa: S104 - test data, nothing binds
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("192.0.2.10", False),  # documentation
        ("198.18.0.1", False),  # benchmarking
        ("224.0.0.1", False),  # multicast
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("fd00:ec2::254", False),  # AWS IPv6 metadata
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped
        ("::ffff:8.8.8.8", True),
        ("2002:a00:1::", False),  # 6to4 wrapping 10.0.0.1
        ("64:ff9b::7f00:1", False),  # NAT64 wrapping 127.0.0.1
        ("64:ff9b::808:808", True),
    ],
)
def test_is_public_ip(address: str, public: bool) -> None:
    assert is_public_ip(ipaddress.ip_address(address)) is public


def fixed_resolver(mapping: dict[str, list[str]]) -> Callable[[str, int], object]:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]

    return resolve


def test_resolve_policy() -> None:
    guard = NetGuard(
        [80, 443],
        resolver=fixed_resolver(  # type: ignore[arg-type]
            {"good.test": ["93.184.215.14"], "mixed.test": ["93.184.215.14", "10.0.0.5"]}
        ),
    )

    async def run() -> None:
        assert await guard.resolve("good.test", 443) == ["93.184.215.14"]
        assert await guard.resolve("8.8.8.8", 80) == ["8.8.8.8"]
        for host, port, message in [
            ("good.test", 8080, "port 8080"),
            ("mixed.test", 443, "non-public"),  # any bad address refuses the host
            ("127.0.0.1", 80, "non-public"),
            ("[::1]", 80, "non-public"),
        ]:
            with pytest.raises(NetGuardError, match=message):
                await guard.resolve(host, port)
        # Not resolving is a network failure (retryable), not a policy refusal.
        with pytest.raises(DnsResolutionError, match="cannot resolve"):
            await guard.resolve("missing.test", 443)

    asyncio.run(run())


def test_leading_zero_ipv4_is_not_a_bypass() -> None:
    # Not an IP literal to Python; the OS resolver may interpret it as 192.168.1.1.
    # Either way it must not be reachable.
    async def run() -> None:
        # Windows fails the lookup; Linux resolves it to 192.168.1.1 and refuses it.
        with pytest.raises((NetGuardError, DnsResolutionError)):
            await NetGuard([80]).resolve("192.168.001.001", 80)

    asyncio.run(run())


@asynccontextmanager
async def http_server() -> AsyncIterator[tuple[int, list[str]]]:
    """Minimal HTTP server on 127.0.0.1 recording the Host header of each request."""
    hosts: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        host = next(
            (line.split(":", 1)[1].strip() for line in head.split("\r\n")
             if line.lower().startswith("host:")),
            "",
        )  # fmt: skip
        hosts.append(host)
        if head.startswith("GET /redirect"):
            location = head.split("GET /redirect?to=", 1)[1].split(" ", 1)[0]
            writer.write(
                f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode()
            )
        else:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nok"
            )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port, hosts


def real_fetcher(guard: NetGuard) -> HttpFetcher:
    return HttpFetcher(FetchSettings(max_retries=0), IDENTITY, UrlSettings(), guard=guard)


def test_real_transport_blocks_loopback_without_connecting() -> None:
    async def run() -> None:
        async with http_server() as (port, hosts):
            async with real_fetcher(NetGuard([port])) as fetcher:
                for url in (f"http://127.0.0.1:{port}/", f"http://localhost:{port}/"):
                    result = await fetcher.fetch(url)
                    assert result.error is FetchError.BLOCKED, url
            assert hosts == []  # the server was never reached

    asyncio.run(run())


def test_connection_goes_to_the_checked_address_with_original_host() -> None:
    """The name resolves only through the guard's resolver, proving the connection is pinned."""

    async def run() -> None:
        async with http_server() as (port, hosts):
            guard = NetGuard(
                [port],
                resolver=fixed_resolver({"company.test": ["127.0.0.1"]}),  # type: ignore[arg-type]
                ip_allowed=lambda ip: True,
            )
            async with real_fetcher(guard) as fetcher:
                result = await fetcher.fetch(f"http://company.test:{port}/")
            assert result.ok and result.body == b"ok"
            assert hosts == [f"company.test:{port}"]

    asyncio.run(run())


def test_redirect_to_disallowed_address_is_blocked_at_the_hop() -> None:
    async def run() -> None:
        async with http_server() as (port, hosts):
            guard = NetGuard(
                [port],
                resolver=fixed_resolver({"company.test": ["127.0.0.1"]}),  # type: ignore[arg-type]
                ip_allowed=lambda ip: str(ip) == "127.0.0.1",
            )
            async with real_fetcher(guard) as fetcher:
                result = await fetcher.fetch(
                    f"http://company.test:{port}/redirect?to=http://127.0.0.2:{port}/"
                )
            assert result.error is FetchError.BLOCKED
            assert result.final_url == f"http://127.0.0.2:{port}/"
            assert len(result.redirect_chain) == 1
            assert len(hosts) == 1  # only the first hop connected

    asyncio.run(run())
