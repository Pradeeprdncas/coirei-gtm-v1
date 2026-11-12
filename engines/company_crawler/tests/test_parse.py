from datetime import UTC, datetime

import lxml.html
import pytest

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.parse.dom import detect_encoding
from gtm_crawler.parse.fingerprint import hamming_distance, simhash
from gtm_crawler.parse.structured import parse_datetime

URL = "https://acme.example/about"

PAGE = b"""<!DOCTYPE html>
<html lang="en-US"><head>
<meta charset="utf-8">
<title>  About Acme \xe2\x80\x94 Inventory software </title>
<meta name="description" content="Acme builds inventory software for retailers.">
<meta property="og:title" content="About Acme">
<meta property="og:image" content="https://acme.example/a.png">
<meta property="og:image" content="https://acme.example/b.png">
<meta property="article:published_time" content="2024-03-05T10:00:00Z">
<link rel="canonical" href="/about/?utm_source=x">
<script>var tracking = "do not index me";</script>
<style>.x { color: red }</style>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "Organization", "name": "Acme Inc", "foundingDate": "2019",
   "founder": {"@type": "Person", "name": "Jane Doe"},
   "address": {"@type": "PostalAddress", "addressLocality": "Austin"}},
  {"@type": ["WebSite", "http://schema.org/Thing"], "url": "https://acme.example/"}
]}
</script>
<script type="application/ld+json">{ this is not json }</script>
<script type="application/ld+json; charset=utf-8"><!--
{"@type": "SoftwareApplication", "name": "Acme Stock", "applicationCategory": "BusinessApplication"}
--></script>
</head>
<body>
<header><nav><ul>
  <li><a href="/products/stock">Stock</a></li>
  <li><a href="/pricing">Pricing</a></li>
  <li><a href="mailto:sales@acme.example">Email</a></li>
  <li><a href="javascript:void(0)">Menu</a></li>
</ul></nav></header>
<main>
  <h1>About <span>Acme</span></h1>
  <p>We serve   more than 500
     customers in <strong>12 countries</strong>.</p>
  <div>Loose intro text <p>Nested paragraph.</p> trailing words</div>
  <p>Read our <a href="https://news.example/acme-raises">funding news</a> today.</p>
  <a href="/team"><img src="t.png" alt="Our team"></a>
  <ol><li>Fast</li><li>Accurate <em>counts</em></li></ol>
  <table>
    <tr><th>Plan</th><th>Price</th></tr>
    <tr><td>Starter</td><td>$29/mo</td></tr>
    <tr><td>Pro</td><td><table><tr><td>inner</td></tr></table></td></tr>
  </table>
  <noscript>Please enable JavaScript</noscript>
  <p><time itemprop="datePublished" datetime="2020-01-01">Jan 1</time></p>
</main>
<footer role="contentinfo"><p>&copy; 2026 Acme Inc, 100 Congress Ave, Austin TX</p></footer>
</body></html>"""


def parse(body: bytes, content_type: str | None = "text/html", **settings: int) -> ParsedPage:
    return parse_page(body, URL, content_type, ParseSettings(**settings), UrlSettings())


@pytest.fixture(scope="module")
def page() -> ParsedPage:
    return parse(PAGE)


def texts(page: ParsedPage) -> list[str]:
    return [b.text for b in page.blocks]


def test_metadata(page: ParsedPage) -> None:
    assert page.parse_error is None
    assert page.title == "About Acme — Inventory software"
    assert page.language == "en-us"
    assert page.canonical_url == "https://acme.example/about/"
    assert page.meta["description"] == ["Acme builds inventory software for retailers."]
    assert page.meta["og:image"] == ["https://acme.example/a.png", "https://acme.example/b.png"]


def test_blocks_keep_all_regions_and_skip_non_content(page: ParsedPage) -> None:
    all_text = page.cleaned_text
    assert "do not index me" not in all_text and "color: red" not in all_text
    assert "enable JavaScript" not in all_text
    assert "We serve more than 500 customers in 12 countries." in texts(page)
    by_text = {b.text: b for b in page.blocks}
    assert by_text["Stock"].region == "nav"  # navigation is kept, labelled
    footer = by_text["© 2026 Acme Inc, 100 Congress Ave, Austin TX"]
    assert footer.region == "footer"
    heading = by_text["About Acme"]
    assert heading.heading_level == 1 and heading.region == "main"


def test_mixed_content_keeps_loose_text_in_order(page: ParsedPage) -> None:
    t = texts(page)
    i = t.index("Loose intro text")
    assert t[i : i + 3] == ["Loose intro text", "Nested paragraph.", "trailing words"]


def test_block_xpaths_locate_the_text_in_the_raw_document(page: ParsedPage) -> None:
    doc = lxml.html.document_fromstring(PAGE)
    for block in page.blocks:
        (element,) = doc.xpath(block.xpath)
        assert element.tag == block.tag
    quote = "more than 500 customers"
    (block,) = [b for b in page.blocks if quote in b.text]
    assert quote in " ".join(doc.xpath(block.xpath)[0].text_content().split())


def test_links(page: ParsedPage) -> None:
    by_url = {link.url: link for link in page.links}
    assert set(by_url) == {
        "https://acme.example/products/stock",
        "https://acme.example/pricing",
        "https://news.example/acme-raises",
        "https://acme.example/team",
    }
    assert page.invalid_links == 2  # mailto: and javascript:
    assert by_url["https://acme.example/pricing"].text == "Pricing"
    assert by_url["https://acme.example/team"].text == "Our team"  # from img alt
    assert by_url["https://news.example/acme-raises"].context == ("Read our funding news today.")


def test_lists_and_tables(page: ParsedPage) -> None:
    ordered = [lst for lst in page.lists if lst.ordered]
    assert ordered[0].items == ("Fast", "Accurate counts")
    outer = page.tables[0]
    assert outer.rows == (("Plan", "Price"), ("Starter", "$29/mo"), ("Pro", "inner"))
    assert page.tables[1].rows == (("inner",),)


def test_json_ld_nodes_with_paths_and_errors(page: ParsedPage) -> None:
    assert len(page.json_ld) == 2  # the malformed script is not kept as data
    assert len(page.json_ld_errors) == 1 and "JSONDecodeError" in page.json_ld_errors[0]
    by_type = {t: n for n in page.json_ld_nodes for t in n.types}
    org = by_type["Organization"]
    assert org.path == '$.jsonld[0]["@graph"][0]'
    assert org.data["name"] == "Acme Inc"
    assert by_type["Person"].path == '$.jsonld[0]["@graph"][0]["founder"]'
    assert by_type["Thing"].path == '$.jsonld[0]["@graph"][1]'  # schema.org IRI prefix removed
    assert by_type["SoftwareApplication"].data["name"] == "Acme Stock"  # comment-wrapped script


def test_published_date_prefers_explicit_markers(page: ParsedPage) -> None:
    assert page.published_at == datetime(2024, 3, 5, 10, tzinfo=UTC)
    assert page.published_at_source == "meta:article:published_time"


def test_published_date_from_json_ld_article() -> None:
    body = b"""<html><head><script type="application/ld+json">
      {"@type": "NewsArticle", "datePublished": "2022-06-01"}</script></head>
      <body><p>x</p></body></html>"""
    p = parse(body)
    assert p.published_at == datetime(2022, 6, 1, tzinfo=UTC)
    assert p.published_at_source == "json-ld:$.jsonld[0].datePublished"


def test_no_publication_marker_means_no_date() -> None:
    p = parse(b"<html><body><p>Posted on March 3, 2024</p></body></html>")
    assert p.published_at is None and p.published_at_source is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-03-05", datetime(2024, 3, 5, tzinfo=UTC)),
        ("2024-03-05T10:00:00+05:30", datetime.fromisoformat("2024-03-05T10:00:00+05:30")),
        ("2024-03-05T10:00:00Z", datetime(2024, 3, 5, 10, tzinfo=UTC)),
        ("March 5, 2024", None),
        ("2024", None),
        ("", None),
        (20240305, None),
    ],
)
def test_parse_datetime(value: object, expected: datetime | None) -> None:
    assert parse_datetime(value) == expected


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (b"<html></html>", "text/html; charset=ISO-8859-1", "cp1252"),
        (b'<meta charset="shift_jis">', "text/html", "shift_jis"),
        (
            b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">',
            None,
            "cp1251",
        ),
        (b"\xef\xbb\xbf<html>", "text/html; charset=latin-1", "utf-8"),  # BOM wins
        (b"<html>", "text/html; charset=bogus", "utf-8"),
        (b"<html>", None, "utf-8"),
    ],
)
def test_detect_encoding(body: bytes, content_type: str | None, expected: str) -> None:
    assert detect_encoding(body, content_type) == expected


def test_decodes_declared_charset() -> None:
    body = "<html><body><p>Café München</p></body></html>".encode("cp1252")
    assert parse(body, "text/html; charset=iso-8859-1").blocks[0].text == "Café München"


def test_xhtml_with_xml_declaration() -> None:
    body = b'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hi</p></body></html>'
    assert texts(parse(body, "application/xhtml+xml")) == ["Hi"]


@pytest.mark.parametrize("body", [b"", b"   ", b"\x00\x01\x02"])
def test_unparseable_input_is_reported_not_raised(body: bytes) -> None:
    p = parse(body)
    assert p.blocks == [] and p.links == []


def test_deeply_nested_markup_does_not_crash() -> None:
    body = b"<html><body>" + b"<div>" * 5000 + b"<p>deep</p>" + b"</div>" * 5000 + b"</body></html>"
    parse(body)


def test_limits_truncate_and_flag() -> None:
    body = (
        b"<html><body>"
        + b"".join(b'<p><a href="/p%d">p%d</a></p>' % (i, i) for i in range(50))
        + b"</body></html>"
    )
    p = parse(body, max_links=10, max_blocks=5)
    assert len(p.links) == 10 and p.links_truncated
    assert len(p.blocks) == 5 and p.blocks_truncated


def test_base_href_is_used_for_links() -> None:
    body = (
        b'<html><head><base href="https://acme.example/docs/"></head>'
        b'<body><a href="guide">g</a></body></html>'
    )
    assert [link.url for link in parse(body).links] == ["https://acme.example/docs/guide"]


def test_simhash_detects_near_duplicates() -> None:
    base = " ".join(f"word{i}" for i in range(300))
    near = base.replace("word150", "changed")
    other = " ".join(f"other{i}" for i in range(300))
    assert hamming_distance(simhash(base), simhash(near)) <= 6
    assert hamming_distance(simhash(base), simhash(other)) > 16
    assert -(2**63) <= simhash(base) < 2**63
    assert simhash("") == 0


def test_date_published_of_non_article_nodes_is_not_the_page_date() -> None:
    # A SoftwareApplication's datePublished is its release date, not when this page was published.
    body = b"""<html><head><script type="application/ld+json">
      {"@type": "SoftwareApplication", "name": "Acme Stock", "datePublished": "2018-01-01"}
      </script></head><body><p>x</p></body></html>"""
    assert parse(body).published_at is None


def test_shared_navigation_does_not_make_pages_duplicates() -> None:
    nav = "<nav>" + "".join(f"<a href='/p{i}'>Menu item {i} products solutions</a>"
                            for i in range(25)) + "</nav>"  # fmt: skip
    foot = "<footer>" + "".join(f"<p>Footer {i} legal privacy terms careers</p>"
                                for i in range(15)) + "</footer>"  # fmt: skip

    def page(main: str, chrome: str = nav + foot) -> ParsedPage:
        doc = f"<html><body>{chrome}<main>{main}</main></body></html>".encode()
        return parse_page(doc, "https://x.com/a", "text/html", ParseSettings(), UrlSettings())

    pricing = page("<h1>Pricing</h1><p>Starter $29 per month.</p>")
    about = page("<h1>About</h1><p>We were founded in 2019 in Austin.</p>")
    assert hamming_distance(pricing.simhash, about.simhash) > 3
    assert pricing.text_sha256 != about.text_sha256
    # The same content under a different nav is still the same page.
    same = page("<h1>Pricing</h1><p>Starter $29 per month.</p>", "<nav><a href='/'>Home</a></nav>")
    assert same.text_sha256 == pricing.text_sha256
    # A page that is all navigation still has a fingerprint.
    assert page("", nav).content_text == page("", nav).cleaned_text != ""


def test_structured_data_urls() -> None:
    from gtm_crawler.parse.structured import JsonLdNode, structured_urls

    nodes = [
        JsonLdNode("$.a", ("Organization",), {
            "url": "https://acme.com/", "@id": "https://acme.com/#org",
            "sameAs": ["https://www.linkedin.com/company/acme", "mailto:x@acme.com"],
            "logo": "https://acme.com/logo.png",
        }),
        JsonLdNode("$.b", ("WebPage",), {"mainEntityOfPage": {"@id": "https://acme.com/about"}}),
    ]  # fmt: skip
    assert structured_urls(nodes) == [
        "https://acme.com/", "https://acme.com/#org",
        "https://www.linkedin.com/company/acme", "https://acme.com/about",
    ]  # fmt: skip
    assert len(structured_urls(nodes, limit=2)) == 2
