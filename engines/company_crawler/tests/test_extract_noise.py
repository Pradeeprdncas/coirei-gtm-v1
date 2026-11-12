"""What the extractors must not pick up (found crawling a real site, 2026-10-03)."""

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.extract.common import Context
from gtm_crawler.extract.company import company_names, extract_company
from gtm_crawler.extract.product import extract_products
from gtm_crawler.extract.signals import extract_signals
from gtm_crawler.extract.traction import extract_traction
from gtm_crawler.parse import ParsedPage, parse_page

from .test_extract_company import ld

DOMAIN = "acme.example"
CTX = Context(DOMAIN, {"Acme"})


def page(url: str, body: str, head: str = "") -> ParsedPage:
    doc = f"<html><head><title>T</title>{head}</head><body>{body}</body></html>".encode()
    return parse_page(doc, url, "text/html", ParseSettings(), UrlSettings())


def values(drafts: list, field: str) -> list[str]:  # type: ignore[type-arg]
    return [d.value_verbatim for d in drafts if d.field == field]


def test_site_name_is_the_company_only_on_its_home_page() -> None:
    def site(url: str, name: str) -> ParsedPage:
        return page(url, "", f'<meta property="og:site_name" content="{name}">')

    home = site("https://acme.example/", "Acme - Robots for warehouses")
    assert company_names(home, DOMAIN) == {"Acme"}  # without its tagline
    assert values(extract_company(home, CTX), "company_name") == ["Acme"]
    for other in (site("https://acme.example/changelog/", "Changelog"),
                  site("https://forum.acme.example/", "Acme Forum")):  # fmt: skip
        assert company_names(other, DOMAIN) == set()
        assert values(extract_company(other, CTX), "company_name") == []


def test_a_sub_sites_organization_node_is_not_the_company() -> None:
    node = {"@type": "Organization", "name": "Acme Developer Docs",
            "url": "https://docs.acme.example"}  # fmt: skip
    docs = page("https://docs.acme.example/security/", "", ld(node))
    assert company_names(docs, DOMAIN) == set()
    assert extract_company(docs, CTX) == []


def test_sections_skip_page_furniture_and_sentences_that_mention_features() -> None:
    body = ("<main><h2>Key features</h2><ul><li>Robot fleets</li></ul>"
            "<h2>Get notified when we ship new features.</h2><ul><li>You might also like</li>"
            "</ul></main><footer><h2>Features</h2><ul><li>Privacy</li></ul></footer>")  # fmt: skip
    drafts = extract_products(page("https://acme.example/platform", body), CTX)
    assert values(drafts, "key_feature") == ["Robot fleets"]
    # An article's features are its subject's, not the company's product's.
    assert extract_products(page("https://acme.example/blog/twitter-tips", body), CTX) == []

    body = ("<main><h1>Seeking to be included in the posted integration partners</h1>"
            "<ul><li>Categories</li></ul><h2>Technology partners</h2><ul><li>Globex</li></ul>"
            "</main><footer><ul><li>Guidelines</li></ul></footer>")  # fmt: skip
    drafts = extract_traction(page("https://acme.example/partners", body), CTX)
    assert values(drafts, "technology_partnership") == ["Globex"]


def test_split_price_cards_and_table_prices() -> None:
    body = ("<h3>Starter</h3><div><div>$</div><p>18</p><p>USD / mo</p><p>Billed yearly</p></div>"
            "<h3>Email</h3><table><tr><td>Newsletters $50/month</td></tr></table>")  # fmt: skip
    drafts = extract_products(page("https://acme.example/pricing/", body), CTX)
    assert values(drafts, "plan_name") == ["Starter"]  # a table's heading is not a plan
    prices = {d.value_verbatim: (d.qualifier, d.value_normalized) for d in drafts
              if d.field == "price"}  # fmt: skip
    assert prices["18"] == ("Starter", {"qualifier": "eq", "amount": 18.0, "currency": "USD",
                                        "per": ["month"], "billing": "annual"})  # fmt: skip
    assert prices["$50/month"][0] is None
    # A docs page about a "price" helper is not a pricing page.
    helper = page("https://docs.acme.example/helpers/price", "<p>Outputs $5 or EUR 5.</p>")
    assert values(extract_products(helper, CTX), "price") == []


def test_negated_partnerships_and_months_as_places() -> None:
    p = page("https://acme.example/faq", "<p>We have no partnership with Mailgun.</p>"
             "<p>We have partnered with Globex.</p>"
             "<p>We close our office in December.</p>"
             "<p>We have offices in London and Berlin.</p>")  # fmt: skip
    assert values(extract_traction(p, CTX), "partnership") == ["Globex"]
    assert values(extract_signals(p, CTX), "office_location") == ["London", "Berlin"]
