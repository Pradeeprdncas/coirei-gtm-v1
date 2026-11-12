"""Identity normalization: comparable, but never collapsing distinct companies."""

import pytest

from gtm_crawler.identity import ExternalId, external_id, normalize_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme", "acme"),
        ("  ACME,  Inc. ", "acme"),
        ("Acme Co., Ltd.", "acme"),
        ("Nestlé S.A.", "nestlé"),
        ("Müller GmbH", "müller"),
        ("Ｆｕｌｌｗｉｄｔｈ Corp", "fullwidth"),  # NFKC
        ("Johnson & Johnson", "johnson and johnson"),
        ("McDonald's Corporation", "mcdonalds"),
        ("STRASSE AG", "strasse"),
        ("Straße AG", "strasse"),  # casefold
    ],
)
def test_normalize_name(name: str, expected: str) -> None:
    assert normalize_name(name) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Acme Holdings Ltd", "Acme Ltd"),  # a holding company is another entity
        ("Acme Labs", "Acme"),
        ("Café Inc", "Cafe Inc"),  # accents are kept
        ("Co", ""),  # a name that is only a legal form stays
        ("Mercury Insurance", "Mercury"),
    ],
)
def test_normalization_does_not_collapse_distinct_names(a: str, b: str) -> None:
    assert normalize_name(a) != normalize_name(b)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/company/Acme-Inc/", ("linkedin", "company/acme-inc")),
        ("https://uk.linkedin.com/company/acme-inc/about/", ("linkedin", "company/acme-inc")),
        ("https://github.com/Acme", ("github", "acme")),
        ("https://github.com/orgs/acme/people", None),
        ("https://github.com/orgs/acme", ("github", "acme")),
        ("https://www.crunchbase.com/organization/acme", ("crunchbase", "organization/acme")),
        ("https://www.g2.com/products/acme/reviews", ("g2", "products/acme")),
        ("https://www.capterra.com/p/12345/Acme/", ("capterra", "12345")),
        ("https://twitter.com/AcmeHQ", ("x", "acmehq")),
        ("https://x.com/@acmehq", ("x", "acmehq")),
        ("https://www.facebook.com/acme", ("facebook", "acme")),
        ("https://www.youtube.com/channel/UCabcDEF", ("youtube", "channel/UCabcDEF")),
        ("https://www.youtube.com/@Acme", ("youtube", "@acme")),
        ("https://www.producthunt.com/products/acme", ("producthunt", "products/acme")),
        ("https://www.glassdoor.com/Overview/Working-at-Acme-EI_IE1234.11,15.htm",
         ("glassdoor", "1234")),
        ("https://pitchbook.com/profiles/company/12345-67", ("pitchbook", "company/12345-67")),
        ("https://angel.co/company/acme", ("wellfound", "company/acme")),
        # Not company profiles.
        ("https://www.linkedin.com/in/jane-doe", None),
        ("https://www.linkedin.com/shareArticle?url=x", None),
        ("https://github.com/acme/widget", None),  # a repository, possibly someone else's
        ("https://github.com/features", None),
        ("https://twitter.com/intent/tweet?text=x", None),
        ("https://twitter.com/acmehq/status/1", None),
        ("https://www.facebook.com/sharer.php?u=x", None),
        ("https://www.producthunt.com/posts/acme-2", None),
        ("https://www.youtube.com/watch?v=abc", None),
        ("https://example.com/company/acme", None),
        ("not a url", None),
    ],
)  # fmt: skip
def test_external_id(url: str, expected: tuple[str, str] | None) -> None:
    assert external_id(url) == (ExternalId(*expected) if expected else None)


def test_same_profile_through_different_urls_is_one_identity() -> None:
    urls = ("https://linkedin.com/company/acme", "https://www.linkedin.com/company/ACME/jobs/",
            "http://de.linkedin.com/company/acme?trk=x")  # fmt: skip
    assert len({external_id(u) for u in urls}) == 1
    assert external_id("https://linkedin.com/company/acme-uk") != external_id(urls[0])
