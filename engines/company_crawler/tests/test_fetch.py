import asyncio
import gzip
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest

from gtm_crawler.config import ConfigError, FetchSettings, IdentitySettings, UrlSettings
from gtm_crawler.fetch.http import FetchError, FetchResult, HttpFetcher, build_user_agent

from .helpers import RecordingSleep, make_fetcher, response, site

HTML = frozenset({"text/html"})
BASE = "https://example.com"


def fetch(handler: object, url: str = f"{BASE}/", allowed: frozenset[str] | None = None,
          sleep: RecordingSleep | None = None, **overrides: object) -> FetchResult:  # fmt: skip
    async def run() -> FetchResult:
        async with make_fetcher(handler, sleep, **overrides) as fetcher:  # type: ignore[arg-type]
            return await fetcher.fetch(url, allowed)

    return asyncio.run(run())


def html(body: bytes = b"<html>ok</html>", **headers: str) -> httpx.Response:
    return response(200, body, **{"Content-Type": "text/html; charset=utf-8", **headers})


def test_success_records_status_headers_and_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return html(**{"Set-Cookie": "sid=secret", "X-Thing": "1"})

    result = fetch(handler, allowed=HTML)
    assert result.ok and result.body == b"<html>ok</html>"
    assert result.status == 200 and result.content_type == "text/html"
    assert result.headers["x-thing"] == "1"
    assert "set-cookie" not in result.headers  # credentials are not stored
    assert result.attempts == 1 and result.redirect_chain == []
    ua = seen[0].headers["user-agent"]
    assert ua.startswith("GTMCrawler/") and "https://crawler.example/contact" in ua
    assert seen[0].headers["accept-encoding"] == "gzip, deflate"


def test_user_agent_contact_is_required() -> None:
    with pytest.raises(ConfigError, match="user_agent_contact"):
        build_user_agent(IdentitySettings())
    with pytest.raises(ConfigError):
        HttpFetcher(FetchSettings(), IdentitySettings(user_agent_contact="  "), UrlSettings())


def test_follows_redirect_chain_resolving_relative_locations() -> None:
    handler = site({
        f"{BASE}/old": httpx.Response(301, headers={"Location": "/older"}),
        f"{BASE}/older": httpx.Response(302, headers={"Location": "https://www.example.com/new#x"}),
        "https://www.example.com/new": html(),
    })  # fmt: skip
    result = fetch(handler, f"{BASE}/old")
    assert result.ok
    assert result.redirect_chain == [f"{BASE}/old", f"{BASE}/older"]
    assert result.final_url == "https://www.example.com/new"


def test_redirect_limits() -> None:
    def endless(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"Location": f"/{n + 1}"})

    result = fetch(endless, max_redirects=3)
    assert result.error is FetchError.TOO_MANY_REDIRECTS
    assert len(result.redirect_chain) == 4

    loop = site({
        f"{BASE}/a": httpx.Response(302, headers={"Location": "/b"}),
        f"{BASE}/b": httpx.Response(302, headers={"Location": "/a"}),
    })  # fmt: skip
    assert fetch(loop, f"{BASE}/a").error is FetchError.REDIRECT_LOOP

    for location in ("mailto:x@example.com", "file:///etc/passwd", "javascript:alert(1)"):
        bad = site({f"{BASE}/": httpx.Response(302, headers={"Location": location})})
        assert fetch(bad).error is FetchError.INVALID_REDIRECT, location


def test_retries_transient_statuses_with_backoff() -> None:
    responses = iter([httpx.Response(503), httpx.Response(502), html()])
    sleep = RecordingSleep()
    result = fetch(lambda r: next(responses), sleep=sleep, backoff_base=2.0)
    assert result.ok and result.attempts == 3
    assert len(sleep.calls) == 2
    assert 1.0 <= sleep.calls[0] <= 2.0 and 2.0 <= sleep.calls[1] <= 4.0  # jittered 2**n


def test_retry_after_is_honoured_or_gives_up() -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), html()])
    sleep = RecordingSleep()
    assert fetch(lambda r: next(responses), sleep=sleep).ok
    assert sleep.calls == [7.0]

    sleep = RecordingSleep()
    result = fetch(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}), sleep=sleep)
    assert result.status == 429 and result.attempts == 1 and sleep.calls == []


def test_retries_exhausted_returns_last_status() -> None:
    result = fetch(lambda r: httpx.Response(503), max_retries=2)
    assert result.status == 503 and result.attempts == 3 and not result.ok
    assert result.error is None


def test_client_errors_are_not_retried() -> None:
    result = fetch(lambda r: httpx.Response(404))
    assert result.status == 404 and result.attempts == 1 and result.body is None


def test_network_errors_retried_then_reported() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = fetch(fail, max_retries=2)
    assert result.error is FetchError.CONNECTION_ERROR and result.attempts == 3


def test_attempt_timeout() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return html()

    result = fetch(slow, attempt_timeout=0.05, max_retries=0)
    assert result.error is FetchError.TOTAL_TIMEOUT


def test_content_type_filter_skips_body() -> None:
    pdf = response(200, b"%PDF", **{"Content-Type": "application/pdf"})
    result = fetch(lambda r: pdf, allowed=HTML)
    assert result.error is FetchError.UNSUPPORTED_CONTENT_TYPE and result.body is None
    assert result.status == 200


def test_declared_oversize_rejected_before_reading() -> None:
    result = fetch(lambda r: html(b"x" * 2000), max_body_bytes=1000)
    assert result.error is FetchError.TOO_LARGE and result.body is None


def test_streamed_oversize_without_content_length() -> None:
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=Chunks())

    assert fetch(handler, max_body_bytes=1000).error is FetchError.TOO_LARGE
    assert fetch(handler, max_body_bytes=20_000).body == b"x" * 10_000


def test_gzip_and_deflate_are_decoded() -> None:
    body = b"<html>" + b"hello " * 100 + b"</html>"
    gz = gzip.compress(body)
    assert fetch(lambda r: html(gz, **{"Content-Encoding": "gzip"})).body == body
    deflated = zlib.compress(body)
    assert fetch(lambda r: html(deflated, **{"Content-Encoding": "deflate"})).body == body


def test_decompression_bomb_is_stopped() -> None:
    bomb = gzip.compress(b"\0" * 50_000_000)  # ~50 KB on the wire
    assert len(bomb) < 100_000
    response = html(bomb, **{"Content-Encoding": "gzip"})
    result = fetch(lambda r: response, max_body_bytes=1_000_000)
    assert result.error is FetchError.TOO_LARGE and result.body is None


def test_unsupported_and_corrupt_encodings() -> None:
    br = html(b"....", **{"Content-Encoding": "br"})
    assert fetch(lambda r: br).error is FetchError.UNSUPPORTED_ENCODING
    corrupt = html(b"definitely not gzip", **{"Content-Encoding": "gzip"})
    assert fetch(lambda r: corrupt).error is FetchError.DECODE


def test_transfer_size_capped_even_when_decoded_output_is_small() -> None:
    # Decompression stops at the end of the gzip member; the trailing bytes
    # still cost bandwidth and must count toward the limit.
    body = gzip.compress(b"<html>small</html>") + b"\0" * 2_000_000
    response_body = html(body, **{"Content-Encoding": "gzip", "Content-Length": ""})
    result = fetch(lambda r: response_body, max_body_bytes=1_000_000)
    assert result.error is FetchError.TOO_LARGE


def test_follow_filter_stops_before_requesting_the_target() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "example.com":
            return response(302, **{"Location": "https://elsewhere.example/landing"})
        return html()

    async def run() -> FetchResult:
        async with make_fetcher(handler) as fetcher:
            return await fetcher.fetch(f"{BASE}/", follow=lambda u: "elsewhere" not in u)

    result = asyncio.run(run())
    assert requested == [f"{BASE}/"]
    assert result.status == 302 and not result.ok and result.error is None
    assert result.final_url == "https://elsewhere.example/landing"
    assert result.redirect_chain == [f"{BASE}/"]
