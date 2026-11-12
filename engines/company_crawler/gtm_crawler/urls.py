"""URL normalization and site identity.

Every URL is normalized before it is deduplicated, queued or stored, so two
spellings of the same resource map to one ``url`` row. Normalization only
applies transformations that do not change which resource is addressed (RFC
3986 section 6 plus removal of configured tracking/session parameters); it does
not follow ``rel=canonical``, which is a page-level claim handled by the parser.

URLs that cannot be crawled safely or meaningfully (non-HTTP schemes, embedded
credentials, malformed hosts or ports) raise ``InvalidUrl`` rather than being
"repaired".
"""

from __future__ import annotations

import ipaddress
import re
from fnmatch import fnmatchcase
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import tldextract

from .config import UrlSettings

DEFAULT_PORTS = {"http": 80, "https": 443}

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
# Characters left as-is when re-quoting a path: RFC 3986 pchar plus "/" and "%"
# (existing escapes were already normalized).
_PATH_SAFE = "/%!$&'()*+,;=:@-._~"
_HOST_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
# A last label WHATWG URL parsing reads as a number makes the host an IPv4
# address in decimal, octal or hex (http://2130706433/, http://0x7f.1/).
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
# WHATWG URL parsing strips these anywhere in the input; links in HTML often contain them.
_STRIPPED_WHITESPACE = re.compile(r"[\t\n\r]")

# Uses the Public Suffix List snapshot bundled with tldextract (no network).
# Private suffixes are included so that e.g. two different *.vercel.app sites
# are not treated as one organization.
_extract = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
)


class InvalidUrl(ValueError):
    """Raised for URLs that cannot be normalized into a crawlable http(s) URL."""


def normalize_url(raw: str, settings: UrlSettings, base: str | None = None) -> str:
    """Return the canonical string form of ``raw`` (resolved against ``base`` if given)."""
    candidate = _STRIPPED_WHITESPACE.sub("", raw).strip()
    if not candidate:
        raise InvalidUrl("empty URL")
    if base is not None:
        candidate = urljoin(base, candidate)

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:  # bad IPv6 brackets, non-numeric or out-of-range port
        raise InvalidUrl(f"malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise InvalidUrl(f"unsupported scheme: {scheme or '(none)'}")
    if parts.username is not None or parts.password is not None:
        raise InvalidUrl("credentials in URL")

    host = _normalize_host(parts.hostname)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = _normalize_path(parts.path, settings)
    query = _normalize_query(parts.query, settings)
    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_domain(host: str) -> str:
    """The registrable domain ("eTLD+1") of ``host``; IPs and bare names map to themselves."""
    return _extract(host).top_domain_under_public_suffix or host


def url_host(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise InvalidUrl(f"URL has no host: {url}")
    return host


def _normalize_host(host: str | None) -> str:
    if not host:
        raise InvalidUrl("missing host")
    host = host.rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidUrl(f"invalid host: {host!r}") from exc
    try:  # IDNA mapping can turn full-width digits into an IPv4 literal
        return ipaddress.ip_address(ascii_host).compressed
    except ValueError:
        pass
    labels = ascii_host.split(".")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        # Resolvers disagree on these (inet_aton reads them as IPv4, others fail),
        # so only the dotted-quad form is accepted: the guard then judges a literal.
        raise InvalidUrl(f"non-canonical IPv4 host: {host!r}")
    if len(ascii_host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise InvalidUrl(f"invalid host: {host!r}")
    return ascii_host


def _normalize_percent_escapes(value: str) -> str:
    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PERCENT_ESCAPE.sub(fix, value)


def _normalize_path(path: str, settings: UrlSettings) -> str:
    if not path:
        return "/"
    segments = [_strip_path_params(seg, settings) for seg in path.split("/")]
    path = "/".join(segments)
    path = quote(_normalize_percent_escapes(path), safe=_PATH_SAFE)
    path = re.sub(r"/{2,}", "/", path)
    path = _remove_dot_segments(path)

    head, _, last = path.rpartition("/")
    if last.lower() in settings.index_files:
        path = head + "/"
    if settings.strip_trailing_slash and len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


def _strip_path_params(segment: str, settings: UrlSettings) -> str:
    """Drop matrix parameters such as ``;jsessionid=...`` that match ``strip_params``."""
    if ";" not in segment:
        return segment
    base, *params = segment.split(";")
    kept = [p for p in params if not _is_stripped(p.partition("=")[0], settings)]
    return ";".join([base, *kept])


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4 for an absolute path."""
    segments = path.split("/")
    output: list[str] = []
    for segment in segments[1:]:
        if segment == ".":
            continue
        if segment == "..":
            if output:
                output.pop()
            continue
        output.append(segment)
    result = "/" + "/".join(output)
    if segments[-1] in (".", "..") and not result.endswith("/"):
        result += "/"
    return result


def _normalize_query(query: str, settings: UrlSettings) -> str:
    if not query:
        return ""
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if not _is_stripped(key, settings)
    ]
    # Parameter order almost never changes the resource; sorting makes
    # ?a=1&b=2 and ?b=2&a=1 deduplicate.
    return urlencode(sorted(pairs), quote_via=quote)


def _is_stripped(name: str, settings: UrlSettings) -> bool:
    name = name.lower()
    return any(fnmatchcase(name, pattern.lower()) for pattern in settings.strip_params)
