import asyncio
import gzip

import httpx

from gtm_crawler.config import RobotsSettings, SitemapSettings, UrlSettings
from gtm_crawler.robots import RobotsCache
from gtm_crawler.sitemaps import SitemapDiscovery, discover_sitemaps, parse_sitemap

from .helpers import make_fetcher, response

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
LIMIT = 10_000_000


def urlset(*entries: str) -> bytes:
    body = "".join(f"<url><loc>{e}</loc></url>" for e in entries)
    return f'<?xml version="1.0"?><urlset {NS}>{body}</urlset>'.encode()


def index(*sitemaps: str) -> bytes:
    body = "".join(f"<sitemap><loc>{s}</loc></sitemap>" for s in sitemaps)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{body}</sitemapindex>'.encode()


def test_parse_urlset_with_metadata() -> None:
    body = f"""<?xml version="1.0"?><urlset {NS}>
      <url><loc> https://example.com/pricing </loc><lastmod>2026-09-01</lastmod>
           <priority>0.9</priority><changefreq>weekly</changefreq></url>
      <url><loc>https://example.com/a</loc><priority>7</priority></url>
      <url><priority>0.5</priority></url>
    </urlset>""".encode()
    parsed = parse_sitemap(body, LIMIT)
    assert [u.url for u in parsed.urls] == ["https://example.com/pricing", "https://example.com/a"]
    first = parsed.urls[0]
    assert (first.lastmod, first.priority, first.changefreq) == ("2026-09-01", 0.9, "weekly")
    assert parsed.urls[1].priority is None  # out of range is ignored, not clamped


def test_parse_index_gzip_and_text() -> None:
    assert parse_sitemap(index("https://example.com/s1.xml"), LIMIT).sitemaps == [
        "https://example.com/s1.xml"
    ]
    gz = gzip.compress(urlset("https://example.com/z"))
    assert [u.url for u in parse_sitemap(gz, LIMIT).urls] == ["https://example.com/z"]
    text = b"https://example.com/one\n\n  https://example.com/two  \n"
    assert [u.url for u in parse_sitemap(text, LIMIT).urls] == [
        "https://example.com/one",
        "https://example.com/two",
    ]


def test_external_entities_are_not_resolved() -> None:
    xxe = f"""<?xml version="1.0"?>
    <!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/hostname">
                      <!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
                      <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>
    <urlset {NS}><url><loc>https://example.com/&xxe;</loc></url>
                <url><loc>https://example.com/&c;</loc></url></urlset>""".encode()
    locs = [u.url for u in parse_sitemap(xxe, LIMIT).urls]
    assert all(len(loc) < 100 for loc in locs)  # neither file content nor expansion
    assert all("aaaaaaaaaa" not in loc for loc in locs)


def discover(routes: dict[str, bytes | int], **settings: int) -> tuple[SitemapDiscovery, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        route = routes.get(url, 404)
        if isinstance(route, int):
            return response(route)
        return response(200, route, **{"Content-Type": "application/xml"})

    async def run() -> SitemapDiscovery:
        async with make_fetcher(handler, max_retries=0) as fetcher:
            robots = RobotsCache(fetcher, RobotsSettings(), "GTMCrawler")
            return await discover_sitemaps(
                "https://example.com/", fetcher, robots, SitemapSettings(**settings), UrlSettings()
            )

    return asyncio.run(run()), requested


def test_discovers_from_robots_and_default_location_through_indexes() -> None:
    result, _ = discover({
        "https://example.com/robots.txt": b"Sitemap: https://example.com/declared.xml\n",
        "https://example.com/declared.xml": index("https://example.com/products.xml.gz"),
        "https://example.com/products.xml.gz": gzip.compress(urlset("https://example.com/p/1")),
        "https://example.com/sitemap.xml": urlset(
            "https://example.com/about?utm_source=x",
            "https://blog.example.com/post",
            "https://other.com/page",
            "not a url",
        ),
    })  # fmt: skip
    urls = {u.url for u in result.urls}
    assert urls == {
        "https://example.com/p/1",
        "https://example.com/about",  # normalized
        "https://blog.example.com/post",  # subdomain is in site
    }
    assert result.dropped == 2  # other.com and the invalid entry
    by_url = {u.url: u for u in result.urls}
    assert by_url["https://example.com/p/1"].sitemap == "https://example.com/products.xml.gz"


def test_off_site_sitemaps_and_failures_are_recorded_not_fatal() -> None:
    result, requested = discover({
        "https://example.com/robots.txt": (
            b"Sitemap: https://evil.com/sitemap.xml\nSitemap: https://example.com/broken.xml\n"
        ),
        "https://example.com/broken.xml": b"<urlset><url><loc>",
        "https://example.com/sitemap.xml": 500,
    })  # fmt: skip
    assert "https://evil.com/sitemap.xml" not in requested
    assert result.failed["https://example.com/sitemap.xml"] == "http_500"
    assert result.urls == []


def test_limits() -> None:
    many = urlset(*(f"https://example.com/{i}" for i in range(20)))
    result, _ = discover({"https://example.com/sitemap.xml": many}, max_urls=5)
    assert len(result.urls) == 5

    chain = {
        "https://example.com/sitemap.xml": index("https://example.com/l1.xml"),
        "https://example.com/l1.xml": index("https://example.com/l2.xml"),
        "https://example.com/l2.xml": index("https://example.com/l3.xml"),
        "https://example.com/l3.xml": urlset("https://example.com/deep"),
    }
    result, requested = discover(chain, max_index_depth=2)  # type: ignore[arg-type]
    assert "https://example.com/l3.xml" not in requested

    result, _ = discover(chain, max_sitemaps=2)  # type: ignore[arg-type]
    assert len(result.fetched) == 2


def test_robots_disallowed_sitemap_is_skipped() -> None:
    result, requested = discover({
        "https://example.com/robots.txt": b"User-agent: *\nDisallow: /sitemap.xml\n",
    })  # fmt: skip
    assert "https://example.com/sitemap.xml" not in requested
    assert result.failed == {"https://example.com/sitemap.xml": "robots_disallowed"}


def test_sitemap_cycles_fetch_each_sitemap_once() -> None:
    result, requested = discover({
        "https://example.com/sitemap.xml": index(
            "https://example.com/sitemap.xml", "https://example.com/a.xml",
            "https://example.com/a.xml",
        ),
        "https://example.com/a.xml": index("https://example.com/sitemap.xml"),
    })  # fmt: skip
    assert requested.count("https://example.com/sitemap.xml") == 1
    assert requested.count("https://example.com/a.xml") == 1
