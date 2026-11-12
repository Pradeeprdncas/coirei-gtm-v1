"""Phase 3 secure network: DNS cache, deployment allowlist, error taxonomy,
TTFB timeouts, decompression ratio, conditional requests, TLS and HTTP/2."""

import asyncio
import gzip
import ipaddress
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import h2.config
import h2.connection
import h2.events
import httpx
import pytest
import trustme

from gtm_crawler.config import ConfigError, FetchSettings, UrlSettings
from gtm_crawler.fetch.http import (
    RETRYABLE_ERRORS,
    FetchError,
    HttpFetcher,
    classify,
)
from gtm_crawler.netguard import (
    DnsResolutionError,
    NetGuard,
    NetGuardError,
    guarded_transport,
    ip_policy,
)

from .helpers import IDENTITY, response

PAGE = b"<html><body><p>Secure page.</p></body></html>"


def fetcher(handler: Any, **settings: Any) -> HttpFetcher:
    options = {"max_retries": 0, **settings}
    return HttpFetcher(FetchSettings(**options), IDENTITY, UrlSettings(),
                       transport=httpx.MockTransport(handler))  # fmt: skip


def run_fetch(f: HttpFetcher, url: str = "https://acme.com/", **kwargs: Any) -> Any:
    async def go() -> Any:
        async with f:
            return await f.fetch(url, **kwargs)

    return asyncio.run(go())


# --- DNS cache and deployment policy -----------------------------------------------


def test_dns_answers_are_cached_bounded_and_rechecked() -> None:
    calls: list[str] = []
    now = [0.0]
    answers = {"a.test": ["93.184.215.14"], "b.test": ["93.184.215.15"],
               "c.test": ["93.184.215.16"], "bad.test": ["10.0.0.1"]}  # fmt: skip

    async def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        return answers[host]

    guard = NetGuard([443], resolver, cache_size=2, cache_seconds=60, clock=lambda: now[0])

    async def go() -> None:
        await guard.resolve("a.test", 443)
        await guard.resolve("a.test", 443)
        assert calls == ["a.test"]  # cached
        now[0] = 61
        await guard.resolve("a.test", 443)
        assert calls == ["a.test", "a.test"]  # expired
        await guard.resolve("b.test", 443)
        await guard.resolve("c.test", 443)  # evicts a.test (size 2)
        await guard.resolve("a.test", 443)
        assert calls.count("a.test") == 3
        for _ in range(2):  # a cached answer is checked every time
            with pytest.raises(NetGuardError):
                await guard.resolve("bad.test", 443)
        assert calls.count("bad.test") == 1

    asyncio.run(go())


def test_allowlist_is_explicit_and_never_opens_metadata() -> None:
    default = ip_policy()
    assert not default(ipaddress.ip_address("10.1.2.3"))
    allowed = ip_policy(["10.0.0.0/8", "169.254.0.0/16", "fd00::/8"])
    assert allowed(ipaddress.ip_address("10.1.2.3"))
    assert allowed(ipaddress.ip_address("93.184.215.14"))
    for metadata in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "169.254.170.2"):
        assert not allowed(ipaddress.ip_address(metadata))
    with pytest.raises(ConfigError, match="allowed_networks"):
        FetchSettings(allowed_networks=("not-a-network",))


def test_certificate_verification_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="verification"):
        guarded_transport(NetGuard([443]), verify=False)


# --- error taxonomy ------------------------------------------------------------------


def _ssl_failure() -> httpx.ConnectError:
    try:
        raise ssl.SSLCertVerificationError("certificate verify failed")
    except ssl.SSLError as cause:
        try:
            raise httpx.ConnectError("handshake failed") from cause
        except httpx.ConnectError as exc:
            return exc


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (DnsResolutionError("nope"), FetchError.DNS_FAILURE),
        (httpx.ConnectTimeout("t"), FetchError.CONNECT_TIMEOUT),
        (httpx.ReadTimeout("t"), FetchError.READ_TIMEOUT),
        (httpx.WriteTimeout("t"), FetchError.READ_TIMEOUT),
        (httpx.PoolTimeout("t"), FetchError.POOL_TIMEOUT),
        (TimeoutError(), FetchError.TOTAL_TIMEOUT),
        (httpx.RemoteProtocolError("bad"), FetchError.PROTOCOL_ERROR),
        (httpx.ConnectError("refused"), FetchError.CONNECTION_ERROR),
        (httpx.ReadError("reset"), FetchError.CONNECTION_ERROR),
        (_ssl_failure(), FetchError.TLS_ERROR),
    ],
)  # fmt: skip
def test_failures_are_classified_precisely(exc: BaseException, error: FetchError) -> None:
    assert classify(exc) is error


def test_retry_follows_the_taxonomy() -> None:
    assert FetchError.DNS_FAILURE in RETRYABLE_ERRORS
    assert FetchError.TLS_ERROR not in RETRYABLE_ERRORS
    assert FetchError.BLOCKED not in RETRYABLE_ERRORS

    def tls(request: httpx.Request) -> httpx.Response:
        raise _ssl_failure()

    result = run_fetch(fetcher(tls, max_retries=3, backoff_base=0.001))
    assert result.error is FetchError.TLS_ERROR and result.attempts == 1  # not retried


def test_slow_response_headers_hit_the_ttfb_timeout() -> None:
    async def slow_headers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return response(200, PAGE, **{"Content-Type": "text/html"})

    result = run_fetch(fetcher(slow_headers, ttfb_timeout=0.05, attempt_timeout=5))
    assert result.error is FetchError.TTFB_TIMEOUT


# --- decompression ratio ----------------------------------------------------------


def gz(body: bytes) -> httpx.Response:
    return response(200, gzip.compress(body),
                    **{"Content-Type": "text/html", "Content-Encoding": "gzip"})  # fmt: skip


def test_decompression_ratio_limit() -> None:
    bomb = b"<html>" + b" " * (3 * 1024 * 1024)  # ~3 MB from a few KB, under max_body_bytes
    result = run_fetch(fetcher(lambda r: gz(bomb)))
    assert result.error is FetchError.DECOMPRESSION_BOMB
    # Ordinary compressed HTML, and small repetitive bodies, are fine.
    normal = b"".join(b"<p>Paragraph %d about pricing and plans.</p>" % i for i in range(20000))
    assert run_fetch(fetcher(lambda r: gz(normal))).body == normal
    small = b"<html>" + b" " * 500_000
    assert run_fetch(fetcher(lambda r: gz(small))).body == small


# --- conditional requests ----------------------------------------------------------


def test_validators_go_to_the_requested_url_only() -> None:
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = {k: v for k, v in request.headers.items()
                                  if k.startswith("if-")}  # fmt: skip
        if request.url.path == "/old":
            return response(301, **{"Location": "https://acme.com/new"})
        return response(304)

    validators = {"etag": '"v1"', "last_modified": "Mon, 01 Sep 2026 00:00:00 GMT"}
    result = run_fetch(fetcher(handler), "https://acme.com/old", validators=validators)
    assert seen["https://acme.com/old"] == {
        "if-none-match": '"v1"', "if-modified-since": "Mon, 01 Sep 2026 00:00:00 GMT"
    }  # fmt: skip
    assert seen["https://acme.com/new"] == {}
    assert result.status == 304 and result.body is None and not result.ok


# --- TLS and HTTP/2 against a real local server -----------------------------------


@asynccontextmanager
async def tls_server(ca: trustme.CA, http2: bool) -> AsyncIterator[int]:
    """HTTPS on 127.0.0.1; speaks HTTP/2 (h2 state machine) when ALPN selects it."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1", "localhost").configure_cert(context)
    context.set_alpn_protocols(["h2", "http/1.1"] if http2 else ["http/1.1"])

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            if ssl_object.selected_alpn_protocol() == "h2":
                conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
                conn.initiate_connection()
                writer.write(conn.data_to_send())
                while data := await reader.read(65535):
                    for event in conn.receive_data(data):
                        if isinstance(event, h2.events.RequestReceived):
                            conn.send_headers(event.stream_id, [
                                (":status", "200"), ("content-type", "text/html"),
                                ("content-length", str(len(PAGE))),
                            ])  # fmt: skip
                            conn.send_data(event.stream_id, PAGE, end_stream=True)
                    writer.write(conn.data_to_send())
                    await writer.drain()
            else:
                await reader.readuntil(b"\r\n\r\n")
                head = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                    f"Content-Length: {len(PAGE)}\r\nConnection: close\r\n\r\n"
                )
                writer.write(head.encode() + PAGE)
                await writer.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def tls_fetcher(port: int, ca: trustme.CA | None, http2: bool) -> HttpFetcher:
    guard = NetGuard([port], ip_allowed=ip_policy(["127.0.0.0/8"]))
    verify: ssl.SSLContext | bool = True
    if ca is not None:
        verify = ssl.create_default_context()
        ca.configure_trust(verify)
    transport = guarded_transport(guard, http2, verify)
    settings = FetchSettings(max_retries=2, backoff_base=0.001)
    return HttpFetcher(settings, IDENTITY, UrlSettings(), transport=transport)


@pytest.mark.parametrize("http2", [True, False])
def test_https_through_the_guard_negotiates_the_protocol(http2: bool) -> None:
    ca = trustme.CA()

    async def go() -> Any:
        async with tls_server(ca, http2=True) as port:
            async with tls_fetcher(port, ca, http2) as f:
                return await f.fetch(f"https://127.0.0.1:{port}/")

    result = asyncio.run(go())
    assert result.ok and result.body == PAGE
    assert result.http_version == ("HTTP/2" if http2 else "HTTP/1.1")


def test_untrusted_certificate_is_a_tls_error_and_not_retried() -> None:
    async def go() -> Any:
        async with tls_server(trustme.CA(), http2=False) as port:
            async with tls_fetcher(port, None, http2=False) as f:
                return await f.fetch(f"https://127.0.0.1:{port}/")

    result = asyncio.run(go())
    assert result.error is FetchError.TLS_ERROR and result.attempts == 1
