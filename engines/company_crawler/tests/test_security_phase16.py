"""Phase 16 security tests (spec sections 9, 28, 29): adversarial input against
the network guard, the fetcher, the browser, the parsers, the queue, the logs
and the API. ``docs/security-review.md`` maps each threat to its control and to
these tests. Run them alone with ``pytest -m security``."""

import asyncio
import gzip
import io
import json
import logging
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, update

from gtm_crawler import deadletter
from gtm_crawler.api import create_app
from gtm_crawler.config import (
    ApiSettings,
    FetchSettings,
    FrontierSettings,
    LoggingSettings,
    ParseSettings,
    RobotsSettings,
    SitemapSettings,
    UrlSettings,
    secret_values,
)
from gtm_crawler.crawl import crawl
from gtm_crawler.db.models import CrawlJob, DeadLetterItem, Page
from gtm_crawler.fetch.browser import _Budget
from gtm_crawler.fetch.http import FetchError, FetchResult, HttpFetcher
from gtm_crawler.frontier import Frontier, FrontierEntry
from gtm_crawler.log import configure_logging
from gtm_crawler.netguard import (
    DnsResolutionError,
    NetGuard,
    NetGuardError,
    ip_policy,
)
from gtm_crawler.parse import parse_page
from gtm_crawler.robots import RobotsCache, RobotsStatus
from gtm_crawler.sitemaps import discover_sitemaps, parse_sitemap
from gtm_crawler.urls import DEFAULT_PORTS, InvalidUrl, normalize_url

from .helpers import IDENTITY, make_fetcher, response
from .test_browser import browser_for, listener, shell  # noqa: F401 - fixture
from .test_crawl import FakeSite, settings_for
from .test_netguard import fixed_resolver, http_server

pytestmark = pytest.mark.security

URLS = UrlSettings()
HTML = frozenset({"text/html"})
# What real DNS answers for these names.
DNS = {
    "localhost": ["127.0.0.1", "::1"],
    "localtest.me": ["127.0.0.1"],  # a public name pointing at loopback
    "metadata.google.internal": ["169.254.169.254"],
    "example.com": ["93.184.215.14"],
}


def outcome(url: str) -> str:
    """What happens to ``url`` before any connection: invalid, blocked or allowed."""
    try:
        url = normalize_url(url, URLS)
    except InvalidUrl:
        return "invalid"
    parts = urlsplit(url)
    guard = NetGuard([80, 443], resolver=fixed_resolver(DNS))  # type: ignore[arg-type]
    try:
        port = DEFAULT_PORTS[parts.scheme] if parts.port is None else parts.port
        asyncio.run(guard.resolve(parts.hostname or "", port))
    except NetGuardError:
        return "blocked"
    except DnsResolutionError:
        return "unresolved"
    return "allowed"


# --- SSRF: address classes and host spellings ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Every non-public class.
        "http://127.0.0.1/", "http://127.255.255.254/", "http://10.0.0.1/",
        "http://172.16.0.1/", "http://172.31.255.255/", "http://192.168.0.1/",
        "http://100.64.0.1/", "http://0.0.0.0/", "http://0.1.2.3/", "http://240.0.0.1/",
        "http://255.255.255.255/", "http://224.0.0.1/", "http://192.0.0.1/",
        "http://198.18.0.1/", "http://192.0.2.1/", "http://[::1]/", "http://[::]/",
        "http://[fe80::1]/", "http://[fc00::1]/", "http://[fd12:3456::1]/", "http://[ff02::1]/",
        "http://[fec0::1]/", "http://[2001:db8::1]/", "http://[100::1]/",
        # Cloud metadata endpoints, by address and by name.
        "http://169.254.169.254/latest/meta-data/", "http://[fd00:ec2::254]/",
        "http://100.100.100.200/", "http://169.254.170.2/v2/credentials",
        "http://metadata.google.internal/computeMetadata/v1/",
        # IPv6 forms embedding a private IPv4 address.
        "http://[::ffff:127.0.0.1]/", "http://[::ffff:a9fe:a9fe]/", "http://[::127.0.0.1]/",
        "http://[::ffff:0:7f00:1]/", "http://[64:ff9b::a9fe:a9fe]/", "http://[64:ff9b:1::a00:1]/",
        "http://[2002:a9fe:a9fe::]/", "http://[2002:7f00:1::]/",
        "http://[2001:0:4136:e378:8000:63bf:80ff:fffe]/",  # Teredo client 127.0.0.1
        # Numeric host spellings (decimal, hex, octal, short, leading zeros).
        "http://2130706433/", "http://0x7f.1/", "http://0x7f000001/", "http://017700000001/",
        "http://127.1/", "http://0/", "http://192.168.001.001/", "http://0x7f.0x0.0x0.0x1/",
        # Case, trailing dot, IDNA mappings to loopback.
        "http://LOCALHOST/", "http://localhost./", "http://127.0.0.1./",
        "http://\u24dbocalhost/", "http://\uff11\uff12\uff17.0.0.1/", "http://127\u30020\u30020\u30021/",
        "http://localtest.me/",
        # User-info and parser-differential tricks.
        "http://evil@127.0.0.1/", "http://user:pw@127.0.0.1/", "http://good.com@127.0.0.1/",
        "http://good.com:80@127.0.0.1/", "http://127.0.0.1#@good.com/",
        "http://127.0.0.1?@good.com/", "http://good.com\\@127.0.0.1/",
        "http://127.0.0.1\\@good.com/", "http://1.1.1.1 &@127.0.0.1# @3.3.3.3/",
        "http://good.com%2f@127.0.0.1/",
        # Ports other than 80/443.
        "http://example.com:22/", "http://example.com:6379/", "https://example.com:0/",
        # Other schemes.
        "file:///etc/passwd", "gopher://127.0.0.1:6379/_FLUSHALL", "dict://127.0.0.1:11211/",
        "ftp://example.com/", "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)", "ws://example.com/", "jar:http://example.com!/",
    ],
)  # fmt: skip
def test_hostile_destinations_never_pass_the_guard(url: str) -> None:
    assert outcome(url) in ("invalid", "blocked")


@pytest.mark.parametrize(
    "url", ["http://2130706433/", "http://0x7f.1/", "http://017700000001/", "http://127.1/"]
)
def test_numeric_ipv4_spellings_are_invalid_whatever_the_resolver(url: str) -> None:
    # glibc's resolver reads these as 127.0.0.1, Windows' fails: neither is relied upon.
    with pytest.raises(InvalidUrl, match="non-canonical IPv4"):
        normalize_url(url, URLS)


def test_idna_mapped_ip_literals_are_normalized_to_the_literal() -> None:
    # Full-width digits and ideographic dots map to an IP literal the guard then judges.
    assert urlsplit(normalize_url("http://１２７.0.0.1/", URLS)).hostname == "127.0.0.1"
    assert urlsplit(normalize_url("http://127。0。0。1/", URLS)).hostname == "127.0.0.1"


def test_public_destinations_still_pass() -> None:
    for url in ("http://example.com/", "https://EXAMPLE.com./a", "http://93.184.215.14/",
                "http://[2606:4700:4700::1111]/", "http://[::ffff:8.8.8.8]/",
                "http://[64:ff9b::808:808]/", "https://1password.com/"):  # fmt: skip
        assert outcome(url) in ("allowed", "unresolved"), url
    assert outcome("http://example.com/") == "allowed"


def test_metadata_is_refused_even_inside_allowed_networks() -> None:
    import ipaddress

    allowed = ip_policy(["169.254.0.0/16", "100.64.0.0/10", "fd00::/8", "::ffff:0:0/96",
                         "64:ff9b::/96", "2002::/16"])  # fmt: skip
    assert allowed(ipaddress.ip_address("169.254.1.1"))  # the allowlist works
    for address in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "169.254.170.2",
                    "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe",
                    "2002:a9fe:a9fe::"):  # fmt: skip
        assert not allowed(ipaddress.ip_address(address)), address


# --- DNS rebinding -------------------------------------------------------------------


def test_dns_rebinding_cannot_swap_the_checked_address() -> None:
    """First answer allowed, every later answer private: the guard resolves once per
    connection, connects to what it checked, and refuses the rebound answer."""

    async def run() -> None:
        async with http_server() as (port, hosts):
            answers = iter([["127.0.0.1"], ["10.0.0.7"], ["10.0.0.7"]])
            lookups: list[str] = []

            async def rebinding(host: str, port: int) -> list[str]:
                lookups.append(host)
                return next(answers)

            guard = NetGuard([port], resolver=rebinding, cache_size=0,
                             ip_allowed=lambda ip: str(ip) == "127.0.0.1")  # fmt: skip
            fetcher = HttpFetcher(FetchSettings(max_retries=0), IDENTITY, URLS, guard=guard)
            async with fetcher:
                first = await fetcher.fetch(f"http://rebind.test:{port}/")
                # A new connection (redirect hop, retry, later page) resolves again.
                second = await fetcher.fetch(f"http://rebind.test:{port}/redirect?to=/x")
            assert first.ok and first.body == b"ok"
            # The first connection is kept alive and reused; any new one is checked.
            assert second.error is FetchError.BLOCKED or len(lookups) == 1
            # The OS resolver never saw the name: the connection went to the checked IP.
            assert hosts and all(h == f"rebind.test:{port}" for h in hosts)

    asyncio.run(run())


def test_dns_rebinding_is_refused_on_a_fresh_connection() -> None:
    async def run() -> None:
        async with http_server() as (port, hosts):
            answers = iter([["127.0.0.1"], ["10.0.0.7"]])

            async def rebinding(host: str, port: int) -> list[str]:
                return next(answers)

            guard = NetGuard([port], resolver=rebinding, cache_size=0,
                             ip_allowed=lambda ip: str(ip) == "127.0.0.1")  # fmt: skip
            for expected in (None, FetchError.BLOCKED):  # one client each: no reuse
                fetcher = HttpFetcher(FetchSettings(max_retries=0), IDENTITY, URLS, guard=guard)
                async with fetcher:
                    result = await fetcher.fetch(f"http://rebind.test:{port}/")
                assert result.error is expected
            assert len(hosts) == 1

    asyncio.run(run())


def test_mixed_answers_are_refused_whole() -> None:
    # A name answering public and private: the OS could pick either, so it is refused.
    guard = NetGuard([80], resolver=fixed_resolver(  # type: ignore[arg-type]
        {"mixed.test": ["93.184.215.14", "::ffff:10.0.0.1"]}))  # fmt: skip
    with pytest.raises(NetGuardError):
        asyncio.run(guard.resolve("mixed.test", 80))


# --- Redirects -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd", "gopher://127.0.0.1:6379/_x", "data:text/html,hi",
        "javascript:alert(1)", "ftp://example.com/", "http://user:pw@example.com/",
        "http://2130706433/", "http://0x7f.1/", "//[::1",
    ],
)  # fmt: skip
def test_redirect_to_a_forbidden_target_is_not_followed(location: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return response(302, Location=location)

    async def run() -> FetchResult:
        async with make_fetcher(handler, max_retries=0) as fetcher:
            return await fetcher.fetch("https://example.com/", HTML)

    result = asyncio.run(run())
    assert result.error is FetchError.INVALID_REDIRECT
    assert seen == ["https://example.com/"]
    assert "pw" not in (result.error_detail or "")


def test_redirect_chain_into_private_and_metadata_addresses_is_blocked_at_the_hop() -> None:
    async def run() -> None:
        async with http_server() as (port, hosts):
            guard = NetGuard([port, 80], resolver=fixed_resolver(  # type: ignore[arg-type]
                {"company.test": ["127.0.0.1"]}),
                ip_allowed=lambda ip: str(ip) == "127.0.0.1")  # fmt: skip
            async with HttpFetcher(FetchSettings(max_retries=0), IDENTITY, URLS,
                                   guard=guard) as fetcher:  # fmt: skip
                for target in (
                    "http://169.254.169.254/latest/meta-data/",
                    f"http://[::ffff:10.0.0.1]:{port}/",
                    f"http://10.0.0.1:{port}/",
                ):
                    hosts.clear()
                    result = await fetcher.fetch(
                        f"http://company.test:{port}/redirect?to={target}"
                    )  # fmt: skip
                    assert result.error is FetchError.BLOCKED, target
                    assert len(hosts) == 1  # only the first hop connected

    asyncio.run(run())


def test_redirect_loops_and_long_chains_stop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("n", "0"))
        if request.url.path == "/loop":
            return response(302, Location="/loop2" if n == 0 else "/loop")
        if request.url.path == "/loop2":
            return response(302, Location="/loop?n=1")
        return response(302, Location=f"/chain?n={n + 1}")

    async def run(url: str) -> FetchResult:
        async with make_fetcher(handler, max_retries=0, max_redirects=5) as fetcher:
            return await fetcher.fetch(url)

    looped = asyncio.run(run("https://example.com/loop"))
    assert looped.error is FetchError.REDIRECT_LOOP
    chain = asyncio.run(run("https://example.com/chain"))
    assert chain.error is FetchError.TOO_MANY_REDIRECTS and len(chain.redirect_chain) == 6


# --- Oversized and compressed payloads -----------------------------------------------


class Endless(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.sent += 65536
            yield b"x" * 65536


def fetch_one(handler: Callable[[httpx.Request], httpx.Response], **settings: Any) -> FetchResult:
    async def run() -> FetchResult:
        async with make_fetcher(handler, max_retries=0, **settings) as fetcher:
            return await fetcher.fetch("https://example.com/", HTML)

    return asyncio.run(run())


def html_response(body: bytes, **headers: str) -> httpx.Response:
    return response(200, body, **{"Content-Type": "text/html", **headers})


def test_endless_body_without_content_length_is_cut_off() -> None:
    stream = Endless()
    result = fetch_one(
        lambda r: httpx.Response(200, headers={"Content-Type": "text/html"}, stream=stream),
        max_body_bytes=1_000_000,
    )
    assert result.error is FetchError.TOO_LARGE and result.body is None
    assert stream.sent <= 1_000_000 + 65536


def test_understated_content_length_does_not_lift_the_limit() -> None:
    body = b"x" * 300_000
    result = fetch_one(lambda r: html_response(body, **{"Content-Length": "10"}),
                       max_body_bytes=100_000)  # fmt: skip
    assert result.error is FetchError.TOO_LARGE


@pytest.mark.parametrize("length", ["-1", "abc", "99999999999999999999", "1, 2"])
def test_malformed_content_length_is_not_trusted(length: str) -> None:
    result = fetch_one(lambda r: html_response(b"x" * 5_000, **{"Content-Length": length}),
                       max_body_bytes=1_000)  # fmt: skip
    assert result.error is FetchError.TOO_LARGE or result.body is None


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("x-gzip", gzip.compress),
    ],
)
def test_compression_bombs_are_stopped(encoding: str, compress: Callable[[bytes], bytes]) -> None:
    bomb = compress(b"\0" * 50_000_000)  # ~50 KB on the wire
    started = time.perf_counter()
    result = fetch_one(lambda r: html_response(bomb, **{"Content-Encoding": encoding}),
                       max_body_bytes=5_000_000, decompression_ratio_min_bytes=100_000)  # fmt: skip
    assert result.error in (FetchError.DECOMPRESSION_BOMB, FetchError.TOO_LARGE)
    assert result.body is None
    assert time.perf_counter() - started < 10


def test_nested_gzip_is_decoded_once_and_bounded() -> None:
    inner = gzip.compress(b"\0" * 20_000_000)
    result = fetch_one(lambda r: html_response(gzip.compress(inner),
                                               **{"Content-Encoding": "gzip"}),
                       max_body_bytes=5_000_000)  # fmt: skip
    # One layer is decoded; the inner gzip is opaque bytes of bounded size.
    assert result.ok and result.body == inner


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip", "gzip, br", "xz"])
def test_unrequested_encodings_are_refused(encoding: str) -> None:
    result = fetch_one(lambda r: html_response(b"\x8b" * 100, **{"Content-Encoding": encoding}))
    assert result.error is FetchError.UNSUPPORTED_ENCODING and result.body is None


@pytest.mark.parametrize(
    "content_type",
    [None, "", "application/octet-stream", "image/svg+xml", "application/x-msdownload",
     "text/html-sandboxed", "text/htmlx", "application/javascript"],
)  # fmt: skip
def test_wrong_content_types_are_not_read(content_type: str | None) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    result = fetch_one(lambda r: response(200, b"<html>MZ</html>", **headers))
    assert result.error is FetchError.UNSUPPORTED_CONTENT_TYPE and result.body is None


def test_content_type_parameters_and_case_do_not_bypass_the_check() -> None:
    ok = fetch_one(lambda r: response(200, b"<p>x</p>", **{"Content-Type": "TEXT/HTML; x=y"}))
    assert ok.ok
    bad = fetch_one(lambda r: response(200, b"<p>x</p>",
                                       **{"Content-Type": "image/png; text/html"}))  # fmt: skip
    assert bad.error is FetchError.UNSUPPORTED_CONTENT_TYPE


def test_oversized_robots_txt_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(200, b"User-agent: *\nAllow: /\n" + b"#" * 2_000_000,
                        **{"Content-Type": "text/plain"})  # fmt: skip

    async def run() -> Any:
        async with make_fetcher(handler, max_retries=0, max_body_bytes=1_000_000) as fetcher:
            return await RobotsCache(fetcher, RobotsSettings(), "GTMCrawler").policy_for(
                "https://example.com/"
            )  # fmt: skip

    policy = asyncio.run(run())
    assert policy.status is RobotsStatus.UNREACHABLE
    assert not policy.can_fetch("https://example.com/pricing")


# --- Parser abuse --------------------------------------------------------------------


def parse(body: bytes, settings: ParseSettings | None = None) -> Any:
    return parse_page(body, "https://example.com/", "text/html", settings or ParseSettings(), URLS)


def test_huge_attributes_and_many_elements_are_bounded() -> None:
    # Small configured caps keep the test fast; the element counts still exceed them.
    settings = ParseSettings(max_links=100, max_blocks=200)
    body = (b'<html><body><p title="' + b"A" * 3_000_000 + b'">x</p>'
            + b"".join(b"<p>para %d</p>" % i for i in range(2_000))
            + b"".join(b'<a href="/l%d">l</a>' % i for i in range(1_000))
            + b"</body></html>")  # fmt: skip
    started = time.perf_counter()
    page = parse(body, settings)
    assert page.parse_error is None
    assert len(page.links) == settings.max_links  # the caps bind
    assert len(page.blocks) == settings.max_blocks
    assert time.perf_counter() - started < 60


def test_json_ld_abuse_is_contained() -> None:
    blocks = [
        '<script type="application/ld+json">{"@type": "Organization", "name": "A"</script>',
        '<script type="application/ld+json">' + "[" * 100_000 + "]" * 100_000 + "</script>",
        '<script type="application/ld+json">{"__proto__": {"x": 1}, "@type": "Thing"}</script>',
        '<script type="application/ld+json">'
        + '{"@type": "T", "a": ' * 500
        + "1"
        + "}" * 500
        + "</script>",
        *['<script type="application/ld+json">{"@type": "Thing", "n": 1}</script>'] * 5_000,
    ]
    page = parse(("<html><head>" + "".join(blocks) + "</head><body><p>x</p></body></html>")
                 .encode())  # fmt: skip
    assert page.parse_error is None
    assert len(page.json_ld_errors) >= 2  # malformed and too deep: recorded, not raised
    assert all(node.path.count("[") <= 70 for node in page.json_ld_nodes)


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
 <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
 <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
 <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/&lol9;</loc></url>
<url><loc>https://example.com/ok</loc></url>
</urlset>"""


def test_billion_laughs_sitemap_is_not_expanded() -> None:
    started = time.perf_counter()
    try:
        urls = [u.url for u in parse_sitemap(BILLION_LAUGHS, 10_000_000).urls]
    except Exception as exc:  # noqa: BLE001 - refusing the document is also safe
        urls = [type(exc).__name__]
    assert all(len(u) < 200 for u in urls)
    assert time.perf_counter() - started < 5


def test_gzip_bomb_sitemap_is_refused() -> None:
    from gtm_crawler.fetch.http import BodyTooLarge

    with pytest.raises(BodyTooLarge):
        parse_sitemap(gzip.compress(b"\0" * 30_000_000), 1_000_000)


def test_sitemap_index_fan_out_is_bounded() -> None:
    children = "".join(f"<sitemap><loc>https://example.com/s{i}.xml</loc></sitemap>"
                       for i in range(500))  # fmt: skip
    index = f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{children}' \
            "</sitemapindex>"  # fmt: skip
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == "https://example.com/sitemap.xml":
            return response(200, index.encode(), **{"Content-Type": "application/xml"})
        if url.endswith(".xml"):
            # Every child is itself an index pointing at more children.
            return response(200, index.replace("/s", f"/{len(requested)}-s").encode(),
                            **{"Content-Type": "application/xml"})  # fmt: skip
        return response(404)

    async def run() -> Any:
        async with make_fetcher(handler, max_retries=0) as fetcher:
            robots = RobotsCache(fetcher, RobotsSettings(), "GTMCrawler")
            return await discover_sitemaps("https://example.com/", fetcher, robots,
                                           SitemapSettings(max_sitemaps=10), URLS)  # fmt: skip

    discovery = asyncio.run(run())
    assert len(discovery.fetched) == 10
    assert len([u for u in requested if u.endswith(".xml")]) == 10


# --- Crawl traps ---------------------------------------------------------------------


def test_trap_flood_admits_a_bounded_number_of_urls() -> None:
    f = Frontier("acme.com", FrontierSettings())
    flood = [
        *(f"/calendar/{y}/{m:02d}/{d:02d}" for y in range(2000, 2040) for m in range(1, 13)
          for d in range(1, 29)),
        *(f"/pricing?sessionid={i:032x}" for i in range(2_000)),
        *(f"/shop?page={i}&sort={i % 7}&color={i % 13}" for i in range(5_000)),
        *("/a" + "/b/c" * i for i in range(1, 200)),
        *(f"/search?q={'x' * i}" for i in range(1, 3_000)),
    ]  # fmt: skip
    admitted = sum(
        f.add(FrontierEntry(f"https://acme.com{path}", 1, 1.0, "link")) is None for path in flood
    )
    assert len(flood) > 17_000
    assert admitted <= 200, admitted


# --- Browser isolation ---------------------------------------------------------------


class GuardedSite(httpx.AsyncBaseTransport):
    """A fake site behind the real guard: every request is checked like a connection."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes, self.served, self.refused = routes, [], []  # type: ignore[var-annotated]
        self.guard = NetGuard(
            [80, 443],
            resolver=fixed_resolver(
                {  # type: ignore[arg-type]
                    "spa.example": ["93.184.215.14"],
                    "internal.spa.example": ["10.0.0.5"],
                }
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            await self.guard.resolve(request.url.host, request.url.port or 443)
        except NetGuardError:
            self.refused.append(str(request.url))
            raise
        self.served.append(str(request.url))
        return self.routes.get(str(request.url).split("?")[0], response(404))


def test_browser_requests_meet_the_guard_and_cannot_escape(
    listener: tuple[int, list[bytes]],  # noqa: F811 - fixture
) -> None:
    port, received = listener
    site = GuardedSite({
        "https://spa.example/robots.txt": response(200, b"User-agent: *\nAllow: /\n"),
        "https://spa.example/go": response(302, Location=f"http://127.0.0.1:{port}/redirected"),
        "https://spa.example/file.bin": response(
            200, b"MZ" * 1000, **{"Content-Type": "application/octet-stream",
                                  "Content-Disposition": "attachment; filename=x.exe"}),
    })  # fmt: skip
    page = shell(
        "<script>"
        "window.open('http://internal.spa.example/popup');"
        f"window.open('http://127.0.0.1:{port}/popup2');"
        "fetch('/go').catch(() => {});"
        "fetch('http://internal.spa.example/api').catch(() => {});"
        "</script>"
        '<script src="https://user:pw@spa.example/creds.js"></script>'
        '<iframe src="file:///etc/passwd"></iframe><iframe src="file:///C:/Windows/win.ini">'
        "</iframe>"
        '<a id="d" href="/file.bin" download>d</a><script>document.getElementById("d").click()'
        "</script>"
    )
    fetcher = HttpFetcher(FetchSettings(max_retries=0), IDENTITY, URLS, transport=site)
    browser = browser_for(fetcher, settle_timeout=3.0)

    async def run() -> FetchResult:
        try:
            return await browser.render(page)
        finally:
            await browser.aclose()
            await fetcher.aclose()

    result = asyncio.run(run())
    assert result.ok, result.error_detail
    assert result.final_url == "https://spa.example/"
    html = (result.body or b"").decode()
    assert "root:" not in html and "[fonts]" not in html
    assert received == []  # nothing reached the loopback listener
    assert not any("internal.spa.example" in u or "127.0.0.1" in u for u in site.served)
    assert not any("creds.js" in u for u in site.served)
    assert all(u.startswith("https://spa.example/") for u in site.served)


class FakeRoute:
    """What ``BrowserFetcher._forward`` needs of a Playwright route."""

    def __init__(self, url: str, resource_type: str) -> None:
        self.request = type("Request", (), {
            "url": url, "resource_type": resource_type, "method": "GET", "headers": {},
            "post_data_buffer": None})()  # fmt: skip
        self.aborted: str | None = None

    async def abort(self, reason: str) -> None:
        self.aborted = reason

    async def fulfill(self, **kwargs: Any) -> None:
        raise AssertionError("must not be fulfilled")


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        # Chromium already drops most of these; the route refuses them regardless.
        ("https://user:pw@spa.example/x.js", "script"),
        ("https://token@spa.example/api", "fetch"),
        ("file:///etc/passwd", "document"),
        ("ftp://spa.example/x", "document"),
        ("https://spa.example/logo.png", "image"),
        ("https://spa.example/file.exe", "other"),
    ],
)
def test_browser_route_refuses_before_any_fetch(url: str, resource_type: str) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return response(200, b"User-agent: *\nAllow: /\n")

    route = FakeRoute(url, resource_type)
    browser = browser_for(make_fetcher(handler))
    asyncio.run(browser._forward(route, _Budget(10, 10**6)))  # type: ignore[arg-type]
    assert route.aborted == "blockedbyclient"
    assert requested == []  # not even robots.txt


# --- Log injection and credential leakage --------------------------------------------


def capture(secrets: list[str] | None = None) -> io.StringIO:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG"), stream=stream, secrets=secrets or [])
    return stream


@pytest.mark.parametrize(
    "hostile",
    ["a\nb", "a\rb", "a\r\nb", "a\u2028b", "a\u2029b", "a\x85b", "a\x0bb", "a\x0cb", "a\x1cb",
     "a\x1eb", '"}\n{"event": "forged"'],
)  # fmt: skip
def test_no_character_splits_a_log_record(hostile: str) -> None:
    stream = capture()
    logging.getLogger("gtm_crawler.test").info(
        "fetch.completed", extra={"url": f"https://example.com/{hostile}", "header": hostile}
    )
    lines = stream.getvalue().splitlines()  # splits on every Unicode line break
    assert len(lines) == 1
    assert json.loads(lines[0])["header"] == hostile


def test_credentials_are_redacted_in_every_form() -> None:
    stream = capture(secrets=["s3cr3t-token", 'q"uote'])
    log = logging.getLogger("gtm_crawler.test")
    log.info("a", extra={"url": "https://ghp_abcdef123@github.com/x"})  # token as user name
    log.info("b", extra={"url": "https://bob:hunter2@example.com/"})
    log.info("c", extra={"detail": "Authorization: bearer s3cr3t-token"})
    log.info("d", extra={"detail": 'config q"uote'})
    log.info("e", extra={"email": "sales@example.com", "path": "https://example.com/@team"})
    out = stream.getvalue()
    for leaked in ("ghp_abcdef123", "hunter2", "bob:", "s3cr3t-token", 'q\\"uote'):
        assert leaked not in out, leaked
    last = json.loads(out.splitlines()[-1])
    assert last == {**last, "email": "sales@example.com", "path": "https://example.com/@team"}


def test_api_token_is_a_redacted_secret() -> None:
    settings = replace(settings_for(Path(".")), api=ApiSettings(token="t0ken-xyz"))  # noqa: S106
    assert "t0ken-xyz" in secret_values(settings)
    assert "t0ken-xyz" not in repr(settings.api)


def test_redirect_with_credentials_does_not_leak_into_logs() -> None:
    stream = capture()

    def handler(request: httpx.Request) -> httpx.Response:
        return response(302, Location="https://admin:hunter2@example.com/next")

    async def run() -> FetchResult:
        async with make_fetcher(handler, max_retries=0) as fetcher:
            return await fetcher.fetch("https://example.com/")

    result = asyncio.run(run())
    assert result.error is FetchError.INVALID_REDIRECT
    assert "hunter2" not in stream.getvalue()
    assert "hunter2" not in (result.error_detail or "")


# --- API: queue poisoning, injection, stored XSS -------------------------------------


def api_client(engine: Engine, tmp_path: Path, **api: Any) -> TestClient:
    settings = replace(settings_for(tmp_path), api=ApiSettings(**api))
    return TestClient(create_app(settings, engine), raise_server_exceptions=False)


def close(engine: Engine, job_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.id == job_id).values(status="failed"))


@pytest.mark.db
def test_malformed_job_requests_are_refused_without_side_effects(
    engine: Engine, tmp_path: Path
) -> None:
    api = api_client(engine, tmp_path)
    with engine.connect() as conn:
        before = len(conn.execute(select(CrawlJob.id)).all())
    bad = [
        {"content": b"not json", "headers": {"Content-Type": "application/json"}},
        {"json": {}},
        {"json": {"root_url": 42}},
        {"json": {"root_url": ["https://a.com"]}},
        {"json": {"root_url": "https://a.com/" + "x" * 5000}},
        {"json": {"root_url": "https://sec-a.com", "idempotency_key": ""}},
        {"json": {"root_url": "https://sec-a.com", "idempotency_key": "k" * 201}},
        {"json": {"root_url": "https://sec-a.com"}, "headers": {"Idempotency-Key": ""}},
        {"json": {"root_url": "https://bob:hunter2@sec-a.com/"}},
        {"json": {"root_url": "javascript:alert(1)"}},
        {"json": {"root_url": "http://2130706433/"}},
        {"json": {"root_url": "https://sec-a.com", "domain": "x" * 300}},
    ]
    for request in bad:
        reply = api.post("/v1/crawls", **request)
        assert reply.status_code == 422, (request, reply.text)
        assert "hunter2" not in reply.text
        assert reply.headers["content-type"].startswith("application/json")
    with engine.connect() as conn:
        assert len(conn.execute(select(CrawlJob.id)).all()) == before


@pytest.mark.db
def test_idempotency_key_cannot_be_replayed_for_another_target(
    engine: Engine, tmp_path: Path
) -> None:
    api = api_client(engine, tmp_path)
    first = api.post("/v1/crawls", json={"root_url": "https://sec-b.com"},
                     headers={"Idempotency-Key": "sec-key-1"})  # fmt: skip
    assert first.status_code == 201
    replay = api.post("/v1/crawls", json={"root_url": "https://sec-b.com"},
                      headers={"Idempotency-Key": "sec-key-1"})  # fmt: skip
    assert replay.status_code == 200 and replay.json()["crawl_id"] == first.json()["crawl_id"]
    # Someone else's key must not hand back (or reveal) a job for another company.
    other = api.post("/v1/crawls", json={"root_url": "https://sec-c.com"},
                     headers={"Idempotency-Key": "sec-key-1"})  # fmt: skip
    assert other.status_code == 409
    assert "sec-b.com" not in other.text
    close(engine, first.json()["crawl_id"])


@pytest.mark.db
def test_injection_in_path_and_query_parameters_is_rejected(engine: Engine, tmp_path: Path) -> None:
    api = api_client(engine, tmp_path)
    job = api.post("/v1/crawls", json={"root_url": "https://sec-d.com"}).json()
    company = job["company_id"]
    for path in ("/v1/crawls/1;DROP TABLE crawl_job", "/v1/crawls/1%20OR%201=1",
                 "/v1/crawls/-1'--", f"/v1/companies/{company}/dossier?x=1'; DROP TABLE url;--",
                 f"/v1/companies/{company}/lineage/founded_year?product_id=1%20OR%201=1",
                 "/v1/dead-letters?limit=1;DELETE FROM dead_letter_item"):  # fmt: skip
        assert api.get(path).status_code in (200, 404, 422), path
    lineage = api.get(f"/v1/companies/{company}/lineage/x' OR '1'='1")
    assert lineage.status_code == 404
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert {"crawl_job", "url", "dead_letter_item"} <= tables
    close(engine, job["crawl_id"])


@pytest.mark.db
def test_every_response_is_inert_json(engine: Engine, tmp_path: Path) -> None:
    api = api_client(engine, tmp_path)
    job = api.post("/v1/crawls", json={"root_url": "https://sec-e.com"}).json()
    paths = [f"/v1/crawls/{job['crawl_id']}", f"/v1/companies/{job['company_id']}/dossier",
             f"/v1/companies/{job['company_id']}/cost",
             f"/v1/companies/{job['company_id']}/lineage/founded_year",
             f"/v1/crawls/{job['crawl_id']}/quality", "/v1/dead-letters", "/v1/crawls/0",
             "/v1/crawls/abc", "/livez", "/metrics", "/nope"]  # fmt: skip
    for path in paths:
        reply = api.get(path)
        assert reply.headers["x-content-type-options"] == "nosniff", path
        assert reply.headers["content-security-policy"].startswith("default-src 'none'"), path
        assert reply.headers["x-frame-options"] == "DENY", path
        assert not reply.headers["content-type"].startswith("text/html"), path
    # No HTML pages at all (Swagger UI and ReDoc are off).
    assert api.get("/docs").status_code == 404 and api.get("/redoc").status_code == 404
    # api.docs (development only) serves Swagger UI and relaxes the CSP for that page alone.
    dev, csp = api_client(engine, tmp_path, docs=True), "content-security-policy"
    assert dev.get("/docs").headers[csp].startswith("default-src 'none'; script-src")
    assert dev.get("/livez").headers[csp] == api.get("/livez").headers[csp]
    close(engine, job["crawl_id"])


@pytest.mark.db
def test_dead_letter_payload_is_data_and_requeue_uses_stored_state(
    engine: Engine, tmp_path: Path
) -> None:
    api = api_client(engine, tmp_path)
    job = api.post("/v1/crawls", json={"root_url": "https://sec-f.com"}).json()
    hostile = {
        "root_url": "http://169.254.169.254/",
        "code": "__import__('os').system('x')",
        "html": "<script>alert(1)</script>",
        "sql": "'); DROP TABLE crawl_job; --",
    }
    with engine.begin() as conn:
        deadletter.record(
            conn,
            "job",
            "crashed",
            job_id=job["crawl_id"],
            error='<img src=x onerror=alert(1)>\n{"forged": 1}',
            payload=hostile,
        )
        conn.execute(update(CrawlJob).where(CrawlJob.id == job["crawl_id"])
                     .values(status="failed"))  # fmt: skip
        item_id = conn.execute(select(DeadLetterItem.id).where(
            DeadLetterItem.job_id == job["crawl_id"])).scalar_one()  # fmt: skip
    listed = api.get("/v1/dead-letters").json()["items"]
    mine = next(i for i in listed if i["id"] == item_id)
    assert mine["payload"] == hostile and mine["error"].startswith("<img")
    requeued = api.post(f"/v1/dead-letters/{item_id}/requeue")
    assert requeued.status_code == 200
    new_job = api.get(f"/v1/crawls/{requeued.json()['requeued']['job_id']}").json()
    # The job is rebuilt from its own stored root, never from the payload.
    assert new_job["root_url"] == "https://sec-f.com/"
    # A resolved item cannot be requeued again (no duplicate work).
    assert api.post(f"/v1/dead-letters/{item_id}/requeue").status_code == 409
    close(engine, new_job["crawl_id"])


# --- Crawled content stored with bound parameters ------------------------------------

INJECTION = "Robert'); DROP TABLE page; -- \\' \" ; SELECT pg_sleep(10); <script>x</script>"


@pytest.mark.db
def test_crawled_injection_strings_are_stored_intact(engine: Engine, tmp_path: Path) -> None:
    site_url = "https://sqli.example"
    title = INJECTION.replace("<", "&lt;").replace(">", "&gt;")
    routes = {
        f"{site_url}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{site_url}/": ("text/html",
                         f"<html><head><title>{title}</title></head><body><p>{title}</p>"
                         f"<a href=\"/q?x=1'%20OR%20'1'='1\">l</a></body></html>".encode()),
    }  # fmt: skip
    started = time.perf_counter()
    summary = asyncio.run(crawl(f"{site_url}/", settings_for(tmp_path), engine,
                                transport=httpx.MockTransport(FakeSite(routes))))  # fmt: skip
    assert summary.status in ("completed", "partial")
    assert time.perf_counter() - started < 10  # pg_sleep never ran
    with engine.connect() as conn:
        titles = conn.execute(select(Page.title).where(Page.title == INJECTION)).scalars().all()
        assert titles == [INJECTION]
        assert "page" in inspect(conn).get_table_names()


# --- End to end: a job aimed at the metadata service ---------------------------------


@pytest.mark.db
def test_crawl_of_a_metadata_address_makes_no_connection(engine: Engine, tmp_path: Path) -> None:
    # No transport override: the real guarded client. Refused before any connect.
    summary = asyncio.run(
        crawl("http://169.254.169.254/latest/meta-data/", settings_for(tmp_path), engine)
    )
    assert summary.status == "failed"
    assert summary.stats.get("pages_parsed", 0) == 0
