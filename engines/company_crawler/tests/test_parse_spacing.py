"""Inline element boundaries (ParseSettings.inline_spacing, parse/dom.py): a space where
adjacent inline elements read as separate words, none where they continue one; legacy
text (PARSER_VERSION 3, inline_spacing off) unchanged; quotes stay re-checkable."""

import pytest

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.evidence import quote_block, resolve
from gtm_crawler.parse import ParsedPage, parse_page

SPACED = ParseSettings(inline_spacing=True)
LEGACY = ParseSettings(inline_spacing=False, skip_aria_hidden=False)


def page(body: str, settings: ParseSettings = SPACED) -> ParsedPage:
    html = f"<html><head><title>t</title></head><body>{body}</body></html>".encode()
    return parse_page(html, "https://acme.example/pricing", "text/html", settings, UrlSettings())


def texts(body: str, settings: ParseSettings = SPACED) -> list[str]:
    return [b.text for b in page(body, settings).blocks]


@pytest.mark.parametrize(
    ("body", "spaced", "legacy"),
    [
        # Adjacent inline elements naming two things.
        (
            "<div><span>EmailOctopus</span><span>Pro</span></div>",
            "EmailOctopus Pro",
            "EmailOctopusPro",
        ),
        ("<div><span>Pro</span><span>$29</span><span>/mo</span></div>", "Pro $29/mo", "Pro$29/mo"),
        # A span laid out as its own line (Tailwind "block") is a word break.
        (
            '<h1>From One <span>URL</span><span class="block mt-1">to Your Growth.</span></h1>',
            "From One URL to Your Growth.",
            "From One URLto Your Growth.",
        ),
        (
            "<div><b>Business</b><span>49</span><span>USD</span></div>",
            "Business 49 USD",
            "Business49USD",
        ),
        # Split amounts and units stay one token.
        ("<p>$<span>29</span><span>/mo</span></p>", "$29/mo", "$29/mo"),
        ("<p>$4<span>.60</span> per month</p>", "$4.60 per month", "$4.60 per month"),
        ("<p><span>1</span><span>,000</span> contacts</p>", "1,000 contacts", "1,000 contacts"),
        ("<p><span>2.9</span><span>%</span> + <span>30</span>¢</p>", "2.9% + 30¢", "2.9% + 30¢"),
        ("<p><span>example</span><span>.com</span></p>", "example.com", "example.com"),
        ("<p><span>Mail</span><span>chimp</span></p>", "Mailchimp", "Mailchimp"),
        # Superscripts and subscripts attach.
        ("<p>$19<sup>99</sup>/mo and H<sub>2</sub>O</p>", "$1999/mo and H2O", "$1999/mo and H2O"),
        # Text separated by punctuation.
        (
            "<li><span>Billed annually.</span><span>Cancel anytime</span></li>",
            "Billed annually. Cancel anytime",
            "Billed annually.Cancel anytime",
        ),
        ("<p><span>(Annual)</span><span>Starter</span></p>", "(Annual) Starter", "(Annual)Starter"),
        # Whitespace already there is kept once; a word split by formatting is one word.
        ("<p>Pro<b> plan</b> costs <em>$9</em>.</p>", "Pro plan costs $9.", "Pro plan costs $9."),
        ("<p>Starter<b>Pack</b></p>", "Starter Pack", "StarterPack"),
        ("<p><b>Starter</b>Pack</p>", "Starter Pack", "StarterPack"),
        # Nested formatting inside a price card.
        (
            "<div><span><b><i>Growth</i></b></span><span><span>€</span><span>19</span>"
            "<sup>,99</sup></span><span>/Monat</span></div>",
            "Growth €19,99/Monat",
            "Growth€19,99/Monat",
        ),
    ],
)
def test_inline_boundaries(body: str, spaced: str, legacy: str) -> None:
    assert texts(body) == [spaced]
    assert texts(body, LEGACY) == [legacy]


def test_a_block_inside_an_inline_element_is_its_own_line() -> None:
    body = "<a href='/p'><div>Starter</div><div>$9</div><div>per month</div></a>"
    assert texts(body) == ["Starter $9 per month"]
    assert texts(body, LEGACY) == ["Starter$9per month"]  # legacy, unchanged


def test_mixed_content_boundaries_are_read_not_padded() -> None:
    # An element with a block child: legacy text pads every inline child with spaces.
    body = "<div>Save<b>20</b>% on <span>$</span><span>29</span><p>Pro plan</p>tail</div>"
    assert texts(body) == ["Save 20% on $29", "Pro plan", "tail"]
    assert texts(body, LEGACY)[0] == "Save 20 % on $ 29"


def test_headings_and_table_cells_match_their_rows() -> None:
    body = (
        "<h3><span>Team</span><span>Plan</span></h3><table><tr><th>Plan</th><th>"
        "<span>Monthly</span><span>Price</span></th></tr><tr><td><b>Business</b>"
        "<span>Plus</span></td><td><span>$</span>49<span>USD</span></td></tr></table>"
    )
    p = page(body)
    assert [b.text for b in p.blocks if b.heading_level] == ["Team Plan"]
    cells = [b.text for b in p.blocks if b.tag in ("td", "th")]
    assert cells == ["Plan", "Monthly Price", "Business Plus", "$49 USD"]
    assert [c for row in p.tables[0].rows for c in row] == cells  # same text, same mode


def test_quotes_resolve_in_the_mode_they_were_taken() -> None:
    body = "<div><span>EmailOctopus</span><span>Pro</span><span>$24</span><span>/mo</span></div>"
    spaced = page(body)
    block = spaced.blocks[0]
    q = quote_block(block, block.text.index("$24"), len(block.text))
    assert q.text == "$24/mo" and resolve(spaced, q) == "$24/mo"
    # The legacy text has other offsets: a quote is only checkable in its own mode.
    assert resolve(page(body, LEGACY), q) != q.text


def test_spaced_text_is_the_default() -> None:
    assert ParseSettings().inline_spacing is True
    assert ParseSettings().skip_aria_hidden is True


def test_aria_hidden_inline_text_is_not_read() -> None:
    # Decoration screen readers skip: a layout placeholder badge, a separator, an icon.
    body = (
        "<h3><span>Team</span><span aria-hidden='true'><span>Placeholder</span></span></h3>"
        "<p>Docs<span aria-hidden='true'> | </span>Blog <i aria-hidden='true'>x</i></p>"
    )
    assert texts(body) == ["Team", "Docs Blog"]
    assert texts(body, LEGACY) == ["TeamPlaceholder", "Docs | Blog x"]
    # A hidden block (a closed modal, an inactive carousel slide) is still read.
    assert texts("<div aria-hidden='true'><p>Quote</p></div>") == ["Quote"]


def test_market_text_versions_keep_their_text() -> None:
    from gtm_crawler.market.collect import parse_settings

    base = ParseSettings()
    for version in (None, "html-text-1", "html-text-2", "html-text-3"):
        assert parse_settings(base, version).skip_aria_hidden is False
