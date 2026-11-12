"""Sitemap discovery and parsing (sitemaps.org protocol).

Starts from sitemaps declared in robots.txt plus ``/sitemap.xml`` and follows
sitemap indexes breadth-first within the configured limits. Handles XML
sitemaps, sitemap indexes, gzip-compressed files and plain-text sitemaps.

Sitemaps are one discovery source among several; a missing or broken sitemap
is logged and skipped, never fatal. Only URLs on the target site are kept.
XML is parsed with entity resolution and network access disabled.

Sitemap requests obey robots.txt and the per-host throttle like any other
request, and every attempt is reported through ``on_fetch`` (the crawl stores
it in ``sitemap_fetch``, with the raw body in the raw store).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from lxml import etree

from .config import SitemapSettings, UrlSettings
from .fetch.http import BodyTooLarge, FetchResult, HttpFetcher, gunzip_limited
from .robots import RobotsCache, origin_of
from .throttle import HostThrottle
from .urls import InvalidUrl, normalize_url, registrable_domain, url_host

log = logging.getLogger(__name__)

SITEMAP_TYPES = frozenset(
    {
        "application/xml", "text/xml", "application/gzip", "application/x-gzip",
        "application/octet-stream", "text/plain",
    }
)  # fmt: skip
_GZIP_MAGIC = b"\x1f\x8b"


@dataclass(frozen=True)
class SitemapUrl:
    url: str
    lastmod: str | None = None
    priority: float | None = None
    changefreq: str | None = None
    sitemap: str | None = None


@dataclass
class ParsedSitemap:
    urls: list[SitemapUrl] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)


@dataclass
class SitemapFetchRecord:
    """One sitemap request and what it yielded."""

    url: str
    depth: int
    parent: str | None  # the sitemap index that listed it (None: robots.txt or /sitemap.xml)
    result: FetchResult | None  # None when policy refused the request
    error: str | None = None
    url_count: int = 0
    child_count: int = 0
    dropped: int = 0


@dataclass
class SitemapDiscovery:
    urls: list[SitemapUrl] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    dropped: int = 0


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, recover=True, remove_comments=True
    )


def parse_sitemap(body: bytes, max_bytes: int) -> ParsedSitemap:
    """Parse one sitemap document (XML urlset, XML sitemapindex, or text)."""
    if body.startswith(_GZIP_MAGIC):
        body = gunzip_limited(body, max_bytes)
    parsed = ParsedSitemap()
    stripped = body.lstrip()
    if not stripped.startswith(b"<"):
        text = stripped.decode("utf-8", "replace")
        parsed.urls = [SitemapUrl(line.strip()) for line in text.splitlines() if line.strip()]
        return parsed

    root = etree.fromstring(body, _xml_parser())
    if root is None:
        return parsed
    kind = etree.QName(root).localname
    for element in root:
        if not isinstance(element.tag, str):
            continue
        values = {etree.QName(child).localname: (child.text or "").strip() for child in element
                  if isinstance(child.tag, str)}  # fmt: skip
        loc = values.get("loc")
        if not loc:
            continue
        if kind == "sitemapindex":
            parsed.sitemaps.append(loc)
        elif kind == "urlset":
            parsed.urls.append(
                SitemapUrl(
                    loc,
                    lastmod=values.get("lastmod") or None,
                    priority=_priority(values.get("priority")),
                    changefreq=values.get("changefreq") or None,
                )
            )
    return parsed


def _priority(raw: str | None) -> float | None:
    try:
        value = float(raw) if raw else None
    except ValueError:
        return None
    return value if value is not None and 0.0 <= value <= 1.0 else None


async def discover_sitemaps(
    site_url: str,
    fetcher: HttpFetcher,
    robots: RobotsCache,
    settings: SitemapSettings,
    url_settings: UrlSettings,
    throttle: HostThrottle | None = None,
    on_fetch: Callable[[SitemapFetchRecord], Awaitable[None]] | None = None,
) -> SitemapDiscovery:
    site = registrable_domain(url_host(site_url))
    policy = await robots.policy_for(site_url)
    origin = origin_of(site_url)

    discovery = SitemapDiscovery()
    queue: deque[tuple[str, int, str | None]] = deque()
    queued: set[str] = set()

    def enqueue(raw: str, depth: int, parent: str | None = None) -> bool:
        url = _in_site(raw, site, url_settings)
        if url is None:
            discovery.dropped += 1
            return False
        if url not in queued:
            queued.add(url)
            queue.append((url, depth, parent))
        return True

    async def report(record: SitemapFetchRecord) -> None:
        if record.error is not None:
            discovery.failed[record.url] = record.error
        if on_fetch is not None:
            await on_fetch(record)

    for declared in policy.sitemaps:
        enqueue(declared, 0)
    enqueue(f"{origin}/sitemap.xml", 0)

    while queue and len(discovery.fetched) < settings.max_sitemaps:
        sitemap_url, depth, parent = queue.popleft()
        sitemap_policy = await robots.policy_for(sitemap_url)
        if not sitemap_policy.can_fetch(sitemap_url):
            await report(SitemapFetchRecord(sitemap_url, depth, parent, None, "robots_disallowed"))
            continue
        if sitemap_policy.delay_too_long:
            await report(
                SitemapFetchRecord(sitemap_url, depth, parent, None, "crawl_delay_too_long")
            )
            continue
        if throttle is not None:
            async with throttle.slot(url_host(sitemap_url), sitemap_policy.crawl_delay):
                result = await fetcher.fetch(sitemap_url, allowed_types=SITEMAP_TYPES)
        else:
            result = await fetcher.fetch(sitemap_url, allowed_types=SITEMAP_TYPES)
        discovery.fetched.append(sitemap_url)
        record = SitemapFetchRecord(sitemap_url, depth, parent, result)
        if not result.ok or result.body is None:
            record.error = str(result.error or f"http_{result.status}")
            await report(record)
            continue
        try:
            parsed = parse_sitemap(result.body, fetcher.settings.max_body_bytes)
        except (etree.XMLSyntaxError, BodyTooLarge, OSError, ValueError) as exc:
            record.error = f"parse_error: {type(exc).__name__}"
            await report(record)
            continue

        if depth < settings.max_index_depth:
            for child in parsed.sitemaps:
                record.child_count += enqueue(child, depth + 1, sitemap_url)
        for entry in parsed.urls:
            if len(discovery.urls) >= settings.max_urls:
                break
            url = _in_site(entry.url, site, url_settings)
            if url is None:
                discovery.dropped += 1
                record.dropped += 1
                continue
            record.url_count += 1
            discovery.urls.append(
                SitemapUrl(url, entry.lastmod, entry.priority, entry.changefreq, sitemap_url)
            )
        await report(record)

    log.info(
        "sitemaps.discovered",
        extra={"site": site, "sitemaps_fetched": len(discovery.fetched),
               "sitemaps_failed": len(discovery.failed), "urls": len(discovery.urls),
               "dropped": discovery.dropped},
    )  # fmt: skip
    return discovery


def _in_site(raw: str, site: str, url_settings: UrlSettings) -> str | None:
    """Normalize an absolute sitemap URL and keep it only if it is on ``site``.

    The protocol requires absolute URLs; relative entries are dropped rather
    than guessed at.
    """
    try:
        url = normalize_url(raw, url_settings)
    except InvalidUrl:
        return None
    return url if registrable_domain(url_host(url)) == site else None
