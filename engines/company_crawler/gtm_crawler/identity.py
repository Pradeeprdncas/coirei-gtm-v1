"""Identity signals: comparable company names and external profile identifiers.

Normalization only makes two values comparable. It is never proof that two
records are the same company: "Mercury" the bank and "Mercury" the insurer
normalize alike. Callers keep the original value next to the normalized one,
and ``db/identity.py`` decides identity from evidence (links between pages we
fetched), not from these strings.

Domains are compared with ``urls.registrable_domain``; nothing here rewrites
them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from .urls import InvalidUrl, registrable_domain, url_host

# Trailing legal forms only. "Holdings", "Group", "Labs" and the like tell
# companies apart and stay.
_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh",
     "ag", "sa", "sas", "sarl", "bv", "nv", "ab", "oy", "oyj", "aps", "srl", "spa", "pty", "pte",
     "kk", "llp", "lp"}
)  # fmt: skip
_DROPPED = re.compile(r"[.'’`]")  # "S.A." -> "sa", "McDonald's" -> "mcdonalds"
_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)


def normalize_name(name: str) -> str:
    """``name`` casefolded, without punctuation or trailing legal forms.

    Accents are kept ("Café" is not "Cafe"), and so is everything that is not
    a legal form: "Acme Holdings Ltd." -> "acme holdings", never "acme".
    """
    text = unicodedata.normalize("NFKC", name).casefold().replace("&", " and ")
    tokens = _SEPARATORS.sub(" ", _DROPPED.sub("", text)).split()
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True)
class ExternalId:
    """A company profile on another platform, e.g. ("linkedin", "company/acme")."""

    source_type: str
    external_id: str


_HANDLE = r"@?(?P<id>[A-Za-z0-9_.-]+)/?$"
# registrable domain -> (source type, path pattern with group "id", first path
# segments that are the platform's own pages). A URL that does not match is not
# a company profile: a person (/in/...), a post, a share button, a repository.
_PROFILES: dict[str, tuple[str, re.Pattern[str], frozenset[str]]] = {
    "linkedin.com": ("linkedin", re.compile(r"/(?P<id>(?:company|showcase|school)/[^/]+)"),
                     frozenset()),
    "github.com": ("github", re.compile(r"/(?:orgs/)?(?P<id>[A-Za-z0-9-]+)/?$"), frozenset(
        {"about", "features", "pricing", "login", "join", "sponsors", "marketplace", "topics",
         "explore", "enterprise", "security", "orgs", "apps", "settings", "site", "contact",
         "collections", "events", "customer-stories", "solutions", "resources", "team",
         "trending", "search", "new", "readme"})),
    "crunchbase.com": ("crunchbase", re.compile(r"/(?P<id>organization/[^/]+)"), frozenset()),
    "g2.com": ("g2", re.compile(r"/(?P<id>(?:products|sellers)/[^/]+)"), frozenset()),
    "capterra.com": ("capterra", re.compile(r"/(?:p|software)/(?P<id>\d+)(?:/|$)"), frozenset()),
    "x.com": ("x", re.compile("/" + _HANDLE), frozenset(
        {"home", "share", "intent", "search", "i", "hashtag", "explore", "login", "signup",
         "privacy", "tos"})),
    "facebook.com": ("facebook", re.compile("/" + _HANDLE), frozenset(
        {"sharer", "sharer.php", "share", "share.php", "pages", "groups", "events", "login",
         "profile.php", "watch", "dialog", "tr", "plugins", "policies", "help"})),
    "instagram.com": ("instagram", re.compile("/" + _HANDLE), frozenset(
        {"p", "reel", "reels", "explore", "accounts", "stories"})),
    "youtube.com": ("youtube", re.compile(r"/(?P<id>@[^/]+|(?:channel|c|user)/[^/]+)"),
                    frozenset()),
    "producthunt.com": ("producthunt", re.compile(r"/(?P<id>products/[^/]+)"), frozenset()),
    "glassdoor.com": ("glassdoor", re.compile(r"EI_IE(?P<id>\d+)"), frozenset()),
    "pitchbook.com": ("pitchbook", re.compile(r"/profiles/(?P<id>company/[0-9-]+)"), frozenset()),
    "wellfound.com": ("wellfound", re.compile(r"/(?P<id>company/[^/]+)"), frozenset()),
}  # fmt: skip
_PROFILES["twitter.com"] = _PROFILES["x.com"]
_PROFILES["angel.co"] = _PROFILES["wellfound.com"]
PROFILE_DOMAINS = tuple(sorted(_PROFILES))
# Identifiers these platforms treat as case-sensitive.
_CASE_SENSITIVE = ("channel/",)


def external_id(url: str) -> ExternalId | None:
    """The company profile ``url`` points to, or None if it is not one we recognise."""
    try:
        rule = _PROFILES.get(registrable_domain(url_host(url)))
    except InvalidUrl:
        return None
    if rule is None:
        return None
    source_type, pattern, reserved = rule
    path = urlsplit(url).path
    m = pattern.match(path) if pattern.pattern.startswith("/") else pattern.search(path)
    if m is None:
        return None
    found = m.group("id").rstrip("/")
    if not found.startswith(_CASE_SENSITIVE):
        found = found.lower()
    if found in reserved or not found:
        return None
    return ExternalId(source_type, found)
