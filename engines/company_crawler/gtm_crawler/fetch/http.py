"""HTTP fetching.

One ``HttpFetcher`` per crawl: it reuses connections, keeps a cookie jar for
that crawl only, and returns a ``FetchResult`` for every URL, including
failures, so the caller can record exactly what happened. It never raises for
network, policy or HTTP errors.

Redirects are followed manually so every hop is normalized, scheme-checked,
loop-checked and counted. Bodies are streamed and decompressed here (not by
httpx) so both the transferred and the decompressed size are bounded, which
defends against oversized responses and decompression bombs.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
import ssl
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

import httpx

from .. import __version__
from ..config import ConfigError, FetchSettings, IdentitySettings, UrlSettings
from ..metrics import (
    BLOCKED_REQUESTS,
    BYTES_DOWNLOADED,
    HTTP_DURATION,
    HTTP_REQUESTS,
    HTTP_RETRIES,
)
from ..netguard import DnsResolutionError, NetGuard, NetGuardError, guarded_transport, ip_policy
from ..urls import InvalidUrl, normalize_url

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Stored with the fetch record; cookies are credentials and are not persisted.
_UNSTORED_HEADERS = frozenset({"set-cookie", "set-cookie2"})


class FetchError(StrEnum):
    """Why a fetch failed (spec section 10: a precise error taxonomy)."""

    # Policy: never retried.
    BLOCKED = "blocked"  # network guard: address, port or scheme not allowed
    INVALID_REDIRECT = "invalid_redirect"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    REDIRECT_LOOP = "redirect_loop"
    # Network: retried.
    DNS_FAILURE = "dns_failure"
    CONNECT_TIMEOUT = "connect_timeout"  # TCP connect or TLS handshake
    TTFB_TIMEOUT = "ttfb_timeout"  # no response headers in time
    READ_TIMEOUT = "read_timeout"  # a read or write stalled
    TOTAL_TIMEOUT = "total_timeout"  # the attempt's wall-clock cap
    POOL_TIMEOUT = "pool_timeout"
    CONNECTION_ERROR = "connection_error"  # refused, reset, closed
    PROTOCOL_ERROR = "protocol_error"  # malformed HTTP
    # Not retried.
    TLS_ERROR = "tls_error"  # handshake or certificate failure
    TOO_LARGE = "too_large"
    DECOMPRESSION_BOMB = "decompression_bomb"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    DECODE = "decode_error"
    RENDER = "render_failed"


RETRYABLE_ERRORS = frozenset({
    FetchError.DNS_FAILURE, FetchError.CONNECT_TIMEOUT, FetchError.TTFB_TIMEOUT,
    FetchError.READ_TIMEOUT, FetchError.TOTAL_TIMEOUT, FetchError.POOL_TIMEOUT,
    FetchError.CONNECTION_ERROR, FetchError.PROTOCOL_ERROR,
})  # fmt: skip


@dataclass
class FetchResult:
    url: str
    final_url: str
    redirect_chain: list[str] = field(default_factory=list)
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    body: bytes | None = None
    error: FetchError | None = None
    error_detail: str | None = None
    attempts: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int | None = None
    # 304 to a conditional request; ``body`` is then the stored earlier copy.
    not_modified: bool = False
    http_version: str | None = None
    # Browser renders only: what the render cost (requests, bytes, refusals).
    render: dict[str, int] | None = None

    @property
    def ok(self) -> bool:
        if self.error is not None or self.status is None:
            return False
        return 200 <= self.status < 300 or (self.not_modified and self.body is not None)


class BodyTooLarge(Exception):
    pass


class DecompressionBomb(Exception):
    pass


class _TtfbTimeout(Exception):
    pass


class UnsupportedEncoding(Exception):
    pass


class LimitedDecoder:
    """Incremental Content-Encoding decoder that refuses to produce more than ``max_bytes``."""

    def __init__(
        self,
        encoding: str,
        max_bytes: int,
        max_ratio: float | None = None,
        ratio_min_bytes: int = 0,
    ) -> None:
        encoding = encoding.strip().lower()
        self._remaining = max_bytes
        self._max_ratio = max_ratio
        self._ratio_min_bytes = ratio_min_bytes
        self._consumed = self._produced = 0
        self._decompressor: zlib._Decompress | None
        if encoding in ("", "identity"):
            self._decompressor = None
        elif encoding in ("gzip", "x-gzip"):
            self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS)
        else:
            raise UnsupportedEncoding(encoding)

    def feed(self, data: bytes) -> bytes:
        if self._decompressor is None:
            return self._take(data)
        self._consumed += len(data)
        out = bytearray()
        while data:
            # max_length bounds each step, so a small compressed chunk cannot
            # expand into a huge allocation before the size check runs.
            chunk = self._decompressor.decompress(data, self._remaining + 1)
            out += self._take(chunk)
            data = self._decompressor.unconsumed_tail
        return bytes(out)

    def _take(self, chunk: bytes) -> bytes:
        self._remaining -= len(chunk)
        if self._remaining < 0:
            raise BodyTooLarge()
        if self._decompressor is not None and self._max_ratio is not None:
            self._produced += len(chunk)
            if (self._produced > self._ratio_min_bytes
                    and self._produced > self._max_ratio * max(self._consumed, 1)):  # fmt: skip
                raise DecompressionBomb(f"{self._produced} bytes from {self._consumed}")
        return chunk


def gunzip_limited(data: bytes, max_bytes: int) -> bytes:
    return LimitedDecoder("gzip", max_bytes).feed(data)


def build_user_agent(identity: IdentitySettings) -> str:
    if not identity.user_agent_contact.strip():
        raise ConfigError(
            "identity.user_agent_contact is required: set a URL or email where site "
            "operators can reach the crawler's operator"
        )
    return f"{identity.product_token}/{__version__} (+{identity.user_agent_contact.strip()})"


class HttpFetcher:
    def __init__(
        self,
        fetch: FetchSettings,
        identity: IdentitySettings,
        urls: UrlSettings,
        guard: NetGuard | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = fetch
        self._urls = urls
        self._sleep = sleep
        self.user_agent = build_user_agent(identity)
        if transport is None:
            guard = guard or NetGuard(
                fetch.allowed_ports, ip_allowed=ip_policy(fetch.allowed_networks),
                cache_size=fetch.dns_cache_size, cache_seconds=fetch.dns_cache_seconds,
            )  # fmt: skip
            if fetch.allowed_networks:
                log.warning("netguard.networks_allowed",
                            extra={"networks": list(fetch.allowed_networks)})  # fmt: skip
            transport = guarded_transport(guard, http2=fetch.http2)
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            # Proxy settings from the environment would route around the guard's
            # view of where connections go.
            trust_env=False,
            timeout=httpx.Timeout(
                fetch.read_timeout,
                connect=fetch.connect_timeout,
                write=fetch.write_timeout,
                pool=fetch.connect_timeout,
            ),  # fmt: skip
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en",
                "Accept-Encoding": "gzip, deflate",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch(
        self,
        url: str,
        allowed_types: frozenset[str] | None = None,
        follow: Callable[[str], bool | Awaitable[bool]] | None = None,
        validators: dict[str, str] | None = None,
    ) -> FetchResult:
        """Fetch ``url`` following redirects.

        ``allowed_types`` limits which bodies are read. ``follow`` decides
        whether a redirect target may be requested; when it declines, the
        result keeps the redirect status and ``final_url`` is the unrequested
        target. ``validators`` (``etag`` / ``last_modified`` from an earlier
        fetch) make the first request conditional; a 304 comes back with
        status 304 and no body, for the caller to use its stored copy.
        """
        started = time.perf_counter()
        result = await self._fetch(url, allowed_types, follow, _conditional(validators))
        elapsed = time.perf_counter() - started
        result.duration_ms = round(elapsed * 1000)
        HTTP_DURATION.observe(elapsed)
        outcome = str(result.error) if result.error else f"{(result.status or 0) // 100}xx"
        HTTP_REQUESTS.labels(outcome).inc()
        BYTES_DOWNLOADED.inc(len(result.body or b""))
        if result.error is FetchError.BLOCKED:
            BLOCKED_REQUESTS.labels("network_guard").inc()
        return result

    async def _fetch(
        self,
        url: str,
        allowed_types: frozenset[str] | None,
        follow: Callable[[str], bool | Awaitable[bool]] | None,
        conditional: dict[str, str] | None = None,
    ) -> FetchResult:
        result = FetchResult(url=url, final_url=url)
        current = url
        while True:
            # Validators belong to the requested URL only, not to redirect targets.
            headers = conditional if current == url else None
            await self._fetch_hop(current, result, allowed_types, headers=headers)
            if result.error or result.status not in REDIRECT_STATUSES:
                break
            location = result.headers.get("location")
            if not location:
                break  # a redirect status without a target is returned as-is
            try:
                target = normalize_url(location, self._urls, base=current)
            except InvalidUrl as exc:
                return self._fail(result, FetchError.INVALID_REDIRECT, str(exc))
            result.redirect_chain.append(current)
            if target in result.redirect_chain:
                return self._fail(result, FetchError.REDIRECT_LOOP, target)
            if len(result.redirect_chain) > self.settings.max_redirects:
                return self._fail(result, FetchError.TOO_MANY_REDIRECTS, target)
            allowed = follow(target) if follow is not None else True
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                result.final_url = target
                log.info("fetch.redirect_not_followed", extra={"url": current, "location": target})
                break
            log.debug("fetch.redirect", extra={"url": current, "location": target})
            current = result.final_url = target

        log.info(
            "fetch.completed",
            extra={
                "url": url,
                "final_url": result.final_url,
                "status": result.status,
                "error": result.error,
                "attempts": result.attempts,
                "bytes": len(result.body) if result.body is not None else None,
            },
        )
        return result

    async def request_once(
        self, method: str, url: str, headers: dict[str, str], content: bytes | None
    ) -> FetchResult:
        """One attempt at one hop: no retries, redirects returned as-is.

        For requests a rendering browser makes; the browser follows redirects
        itself and each hop comes back through here, so the guard and size
        limits apply to all of them.
        """
        result = FetchResult(url=url, final_url=url)
        await self._fetch_hop(
            url, result, None, retries=0, method=method, headers=headers, content=content
        )
        return result

    async def _fetch_hop(
        self,
        url: str,
        result: FetchResult,
        allowed_types: frozenset[str] | None,
        retries: int | None = None,
        **request: Any,
    ) -> None:
        retries = self.settings.max_retries if retries is None else retries
        for attempt in range(retries + 1):
            result.attempts += 1
            result.error = result.error_detail = None
            result.body = None
            delay: float | None = None
            try:
                async with asyncio.timeout(self.settings.attempt_timeout):
                    await self._request(url, result, allowed_types, **request)
            except NetGuardError as exc:
                self._fail(result, FetchError.BLOCKED, str(exc))
                return
            except httpx.InvalidURL as exc:
                # httpx parses a redirect's Location even with follow_redirects
                # off, so a hostile Location header surfaces here.
                self._fail(result, FetchError.INVALID_REDIRECT, str(exc))
                return
            except (DnsResolutionError, _TtfbTimeout, TimeoutError, httpx.TransportError) as exc:
                error = classify(exc)
                self._fail(result, error, f"{type(exc).__name__}: {exc}"[:500])
                if error not in RETRYABLE_ERRORS:
                    return
                delay = self._backoff(attempt)
            else:
                if result.status not in RETRY_STATUSES:
                    return
                delay = self._retry_after(result.headers.get("retry-after"), attempt)
                if delay is None:
                    return  # server asked for longer than we are willing to wait

            if attempt == retries:
                return
            HTTP_RETRIES.labels("in_fetch").inc()
            log.info(
                "fetch.retry",
                extra={"url": url, "attempt": attempt + 1, "delay": round(delay, 2),
                       "status": result.status, "error": result.error},
            )  # fmt: skip
            await self._sleep(delay)

    async def _request(
        self,
        url: str,
        result: FetchResult,
        allowed_types: frozenset[str] | None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> None:
        async with contextlib.AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(self.settings.ttfb_timeout):
                    response = await stack.enter_async_context(
                        self._client.stream(method, url, headers=headers, content=content)
                    )
            except TimeoutError as exc:
                raise _TtfbTimeout(
                    f"no response headers within {self.settings.ttfb_timeout}s"
                ) from exc
            await self._read_response(response, result, allowed_types)

    async def _read_response(
        self,
        response: httpx.Response,
        result: FetchResult,
        allowed_types: frozenset[str] | None,
    ) -> None:
        result.status = response.status_code
        result.http_version = response.http_version
        result.headers = {
            k.lower(): v
            for k, v in response.headers.multi_items()
            if k.lower() not in _UNSTORED_HEADERS
        }
        result.content_type = _media_type(response.headers.get("content-type"))
        if not 200 <= response.status_code < 300:
            return
        if allowed_types is not None and result.content_type not in allowed_types:
            self._fail(result, FetchError.UNSUPPORTED_CONTENT_TYPE, result.content_type)
            return
        limit = self.settings.max_body_bytes
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            self._fail(result, FetchError.TOO_LARGE, f"content-length {declared}")
            return
        try:
            decoder = LimitedDecoder(
                response.headers.get("content-encoding", ""), limit,
                self.settings.max_decompression_ratio,
                self.settings.decompression_ratio_min_bytes,
            )  # fmt: skip
            received = 0
            body = bytearray()
            async for chunk in response.aiter_raw():
                received += len(chunk)
                if received > limit:
                    raise BodyTooLarge()
                body += decoder.feed(chunk)
        except BodyTooLarge:
            self._fail(result, FetchError.TOO_LARGE, f"exceeds {limit} bytes")
            return
        except DecompressionBomb as exc:
            self._fail(result, FetchError.DECOMPRESSION_BOMB, str(exc))
            return
        except UnsupportedEncoding as exc:
            self._fail(result, FetchError.UNSUPPORTED_ENCODING, str(exc))
            return
        except zlib.error as exc:
            self._fail(result, FetchError.DECODE, str(exc))
            return
        result.body = bytes(body)

    def _backoff(self, attempt: int) -> float:
        base = min(self.settings.backoff_max, self.settings.backoff_base * 2.0**attempt)
        return base * random.uniform(0.5, 1.0)  # noqa: S311 - jitter, not security

    def _retry_after(self, header: str | None, attempt: int) -> float | None:
        seconds = retry_after_seconds(header)
        if seconds is None:
            return self._backoff(attempt)
        return None if seconds > self.settings.max_retry_after else seconds

    @staticmethod
    def _fail(result: FetchResult, error: FetchError, detail: str | None) -> FetchResult:
        result.error = error
        result.error_detail = detail
        result.body = None
        return result


def retry_class(result: FetchResult) -> str | None:
    """The retry policy class of a failed fetch (spec section 23), None if it is
    not worth retrying: "rate_limited" (429, 503), "dns" or "transient"."""
    if result.error is None:
        if result.status in (429, 503):
            return "rate_limited"
        return "transient" if result.status in RETRY_STATUSES else None
    if result.error is FetchError.DNS_FAILURE:
        return "dns"
    return "transient" if result.error in RETRYABLE_ERRORS else None


def classify(exc: BaseException) -> FetchError:
    """The taxonomy entry for a network-level failure of one attempt."""
    if isinstance(exc, DnsResolutionError):
        return FetchError.DNS_FAILURE
    if isinstance(exc, _TtfbTimeout):
        return FetchError.TTFB_TIMEOUT
    if isinstance(exc, httpx.ConnectTimeout):
        return FetchError.CONNECT_TIMEOUT
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout):
        return FetchError.READ_TIMEOUT
    if isinstance(exc, httpx.PoolTimeout):
        return FetchError.POOL_TIMEOUT
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return FetchError.TOTAL_TIMEOUT
    if isinstance(exc, httpx.RemoteProtocolError) and "location header" in str(exc).lower():
        # httpx parses Location even with follow_redirects off: a hostile
        # redirect target, not a transient protocol failure worth retrying.
        return FetchError.INVALID_REDIRECT
    if isinstance(exc, httpx.ProtocolError):
        return FetchError.PROTOCOL_ERROR
    cause: BaseException | None = exc
    while cause is not None:  # a TLS failure surfaces as ConnectError caused by SSLError
        if isinstance(cause, ssl.SSLError):
            return FetchError.TLS_ERROR
        cause = cause.__cause__ or cause.__context__
    return FetchError.CONNECTION_ERROR


def _conditional(validators: dict[str, str] | None) -> dict[str, str] | None:
    if not validators:
        return None
    headers = {}
    if validators.get("etag"):
        headers["If-None-Match"] = validators["etag"]
    if validators.get("last_modified"):
        headers["If-Modified-Since"] = validators["last_modified"]
    return headers or None


def retry_after_seconds(header: str | None) -> float | None:
    """Seconds a ``Retry-After`` header asks for (delta or HTTP date); None if absent/invalid."""
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        seconds = (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None
