"""Phase 5: main text, metadata, microdata, and parser robustness."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.db.models import Fetch, Page, Url
from gtm_crawler.parse import PARSER_VERSION, ParsedPage, parse_page
from gtm_crawler.parse.fingerprint import hamming_distance

from .test_crawl import SITE, FakeSite, crawl_urls, html, run, settings_for


def parse(body: str | bytes) -> ParsedPage:
    raw = body.encode() if isinstance(body, str) else body
    return parse_page(raw, "https://acme.com/", "text/html", ParseSettings(), UrlSettings())


MENU = "".join(f'<a href="/p{i}">Product line number {i}</a>' for i in range(40))
CHROME = (
    f'<div class="site-menu">{MENU}</div>'
    '<div id="cookie-banner"><p>We use cookies to improve your experience.</p></div>'
    '<div class="footer-links"><p>Acme Inc, 1 Main Street, Austin, Texas.</p></div>'
)


def div_soup(content: str) -> str:
    return f"<html><body>{CHROME}<div class='content'><p>{content}</p></div></body></html>"


def test_div_layout_boilerplate_is_not_main_text() -> None:
    pricing = parse(div_soup("Starter costs $29 per month for five users."))
    about = parse(div_soup("Acme was founded in 2019 to fix inventory."))
    assert pricing.main_text == "Starter costs $29 per month for five users."
    # Everything is still there for evidence.
    assert "Acme Inc, 1 Main Street" in pricing.cleaned_text
    # Pages sharing only their chrome are not duplicates.
    assert pricing.text_sha256 != about.text_sha256
    assert hamming_distance(pricing.simhash, about.simhash) > 3
    regions = {b.text[:20]: b.region for b in pricing.blocks}
    assert regions["We use cookies to im"] == "aside"
    assert regions["Acme Inc, 1 Main Str"] == "footer"


def test_hints_match_whole_tokens_only() -> None:
    page = parse("<html><body><div class='headerless navigator'><p>Real content.</p></div>"
                 "</body></html>")  # fmt: skip
    assert page.blocks[0].region is None and page.main_text == "Real content."


def test_description_headings_and_microdata() -> None:
    page = parse("""<html><head>
        <meta property="og:description" content="Inventory software for retailers.">
        </head><body>
        <h1>Acme</h1><h2>Pricing</h2><p>Text.</p>
        <div itemscope itemtype="https://schema.org/Organization">
          <span itemprop="name">Acme Inc</span>
          <a itemprop="url" href="https://acme.com/">site</a>
          <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
            <span itemprop="addressLocality">Austin</span>
            <span itemprop="addressCountry" content="US">United States</span>
          </div>
        </div></body></html>""")
    assert page.description == "Inventory software for retailers."
    assert page.headings == [(1, "Acme"), (2, "Pricing")]
    org, address = page.microdata
    assert org.types == ("Organization",)
    assert org.properties["name"] == ["Acme Inc"]
    assert org.properties["url"] == ["https://acme.com/"]
    assert "addressLocality" not in org.properties  # it belongs to the nested item
    assert address.types == ("PostalAddress",)
    assert address.properties == {"addressLocality": ["Austin"], "addressCountry": ["US"]}
    assert address.xpath.startswith(org.xpath)


@pytest.mark.parametrize("depth", [300, 100_000])
def test_pathological_nesting_is_bounded_and_reported(depth: int) -> None:
    body = "<html><body>" + "<div>" * depth + "<p>deep</p>" + "</div>" * depth + "</body></html>"
    page = parse(body)
    assert page.parse_error is None
    assert page.parse_warnings and "depth" in page.parse_warnings[0].lower()


def test_an_unexpected_parser_failure_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gtm_crawler.parse as parse_module

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(parse_module, "extract_dom", explode)
    page = parse("<html><body><p>x</p></body></html>")
    assert page.parse_error == "RuntimeError: parser bug" and page.blocks == []


BROKEN_ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html("Acme", '<a href="/empty">Empty</a><a href="/ok">OK</a>')),
    f"{SITE}/empty": ("text/html", b"   "),
    f"{SITE}/ok": ("text/html", html("OK", "<p>Fine page.</p>")),
}


@pytest.mark.db
def test_unparseable_page_keeps_its_raw_artifact_and_the_crawl_goes_on(
    engine: Engine, tmp_path: Path
) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite(BROKEN_ROUTES))
    assert summary.status == "completed"
    rows = crawl_urls(engine, summary.crawl_id)
    assert rows[f"{SITE}/ok"].state == "processed"
    with engine.connect() as conn:
        page = conn.execute(
            select(Page.meta, Page.parser_version, Fetch.raw_sha256)
            .join(Fetch, Fetch.id == Page.fetch_id)
            .join(Url, Url.id == Fetch.url_id)
            .where(Fetch.crawl_id == summary.crawl_id, Url.url == f"{SITE}/empty")
        ).one()
    assert page.meta["parse_error"] and page.raw_sha256  # raw kept for replay
    assert page.parser_version == PARSER_VERSION


@pytest.mark.db
def test_page_metadata_is_persisted(engine: Engine, tmp_path: Path) -> None:
    routes = {
        f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{SITE}/": ("text/html", html(
            "Acme", '<h1>Acme</h1><div itemscope itemtype="https://schema.org/Product">'
            '<span itemprop="name">Stock</span></div>',
            '<meta name="description" content="Inventory software.">')),
    }  # fmt: skip
    summary = run(engine, settings_for(tmp_path), FakeSite(routes))
    with engine.connect() as conn:
        meta = conn.execute(
            select(Page.meta).join(Fetch, Fetch.id == Page.fetch_id)
            .where(Fetch.crawl_id == summary.crawl_id)
        ).scalar_one()  # fmt: skip
    assert meta["description"] == "Inventory software."
    assert meta["headings"] == [[1, "Acme"]]
    assert meta["microdata"][0]["types"] == ["Product"]
    assert meta["microdata"][0]["properties"] == {"name": ["Stock"]}
