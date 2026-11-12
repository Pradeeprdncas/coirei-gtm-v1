import pytest

from gtm_crawler.config import UrlSettings
from gtm_crawler.urls import InvalidUrl, normalize_url, registrable_domain, url_host

DEFAULT = UrlSettings()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # scheme / host case, default ports, fragments
        ("HTTPS://Example.COM", "https://example.com/"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com./a", "https://example.com/a"),
        # whitespace that browsers strip
        ("  https://example.com/a\n/b\t ", "https://example.com/a/b"),
        # dot segments and duplicate slashes
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/a/b/..", "https://example.com/a/"),
        ("https://example.com/../../a", "https://example.com/a"),
        ("https://example.com//a///b", "https://example.com/a/b"),
        ("https://example.com/a/%2E%2E/b", "https://example.com/b"),
        # percent-encoding: unreserved decoded, reserved kept and uppercased, unsafe encoded
        ("https://example.com/%7euser/%61", "https://example.com/~user/a"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/café", "https://example.com/caf%C3%A9"),
        # index files; trailing slash kept by default
        ("https://example.com/docs/index.html", "https://example.com/docs/"),
        ("https://example.com/docs/INDEX.HTM", "https://example.com/docs/"),
        ("https://example.com/about/", "https://example.com/about/"),
        # query: tracking/session params removed, remaining sorted
        ("https://example.com/p?utm_source=x&b=2&a=1&gclid=z", "https://example.com/p?a=1&b=2"),
        ("https://example.com/p?UTM_Campaign=x", "https://example.com/p"),
        ("https://example.com/p?q=a+b", "https://example.com/p?q=a%20b"),
        ("https://example.com/p?flag", "https://example.com/p?flag="),
        # session ids in path parameters
        ("https://example.com/cart;jsessionid=ABC123", "https://example.com/cart"),
        ("https://example.com/a;v=1", "https://example.com/a;v=1"),
        # IDN and IP hosts
        ("https://münchen.de/", "https://xn--mnchen-3ya.de/"),
        ("http://[0:0::1]/", "http://[::1]/"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_url(raw, DEFAULT) == expected


def test_normalization_is_idempotent() -> None:
    once = normalize_url("HTTPS://Ex.com/a/../b%7e?z=1&utm_x=2&a=%41#f", DEFAULT)
    assert normalize_url(once, DEFAULT) == once


def test_relative_links_resolve_against_base() -> None:
    base = "https://example.com/docs/guide/intro"
    assert normalize_url("../api", DEFAULT, base) == "https://example.com/docs/api"
    assert normalize_url("/pricing", DEFAULT, base) == "https://example.com/pricing"
    assert normalize_url("//cdn.example.com/x", DEFAULT, base) == "https://cdn.example.com/x"
    assert normalize_url("?page=2", DEFAULT, base) == "https://example.com/docs/guide/intro?page=2"


def test_trailing_slash_stripping_is_configurable() -> None:
    settings = UrlSettings(strip_trailing_slash=True)
    assert normalize_url("https://example.com/about/", settings) == "https://example.com/about"
    assert normalize_url("https://example.com/", settings) == "https://example.com/"


def test_strip_params_are_configurable() -> None:
    settings = UrlSettings(strip_params=("ref",))
    assert normalize_url("https://e.com/?ref=x&utm_source=y", settings) == (
        "https://e.com/?utm_source=y"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "mailto:sales@example.com",
        "javascript:alert(1)",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "data:text/html,hi",
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https://example.com:99999/",
        "https://example.com:abc/",
        "http://[::1/",
        "https:///path-only",
        "https://exa mple.com/",
        "https://-bad-.com/",
        "https://" + "a" * 64 + ".com/",
        "example.com/no-scheme",
        "http://192.168.001.1/",  # non-canonical IPv4 spelling (Phase 16)
    ],
)
def test_rejects_uncrawlable_urls(raw: str) -> None:
    with pytest.raises(InvalidUrl):
        normalize_url(raw, DEFAULT)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.com", "example.com"),
        ("www.docs.example.com", "example.com"),
        ("shop.example.co.uk", "example.co.uk"),
        # private suffixes: separate tenants are separate sites
        ("acme.vercel.app", "acme.vercel.app"),
        ("docs.acme.github.io", "acme.github.io"),
        ("192.0.2.1", "192.0.2.1"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_url_host() -> None:
    assert url_host("https://docs.example.com/a") == "docs.example.com"
