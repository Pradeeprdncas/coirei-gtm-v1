"""External web search (spec section 14; Exa, approved 2026-09-29).

Search only finds URLs. Every result page is then fetched by our own crawler
(robots.txt, network guard, throttle, raw store), never taken from the
search provider's copy, so evidence always comes from a page we retrieved.

Providers implement ``SearchProvider`` and return normalized results: http(s)
URLs only, each once, in the provider's order with its position. A provider
retries transient failures itself (network errors, 429, 5xx; jittered
backoff, honouring Retry-After up to ``max_retry_after``). ``FailoverSearch``
puts the configured providers behind one interface: it asks them in order,
skips a provider whose circuit is open (``breaker.CircuitBreaker``) and falls
over to the next when one fails. Only Exa is implemented today; another
provider plugs in by implementing ``SearchProvider`` and being listed in
``make_search``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import __version__
from .breaker import CircuitBreaker
from .config import SearchSettings
from .fetch.http import retry_after_seconds
from .metrics import SEARCH_REQUESTS

log = logging.getLogger(__name__)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_HOMEPAGE = re.compile(r"^(?:-\s*)?Homepage:\s*((?:[a-z0-9-]+\.)+[a-z]{2,})$", re.IGNORECASE)


class SearchError(Exception):
    pass


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str | None
    published: str | None
    # 0-based rank in the provider's list, after normalization.
    position: int = 0


@dataclass(frozen=True)
class PageContents:
    """A provider's copy of one page's text (not a page we fetched)."""

    url: str
    title: str | None
    text: str

    def to_html(self) -> bytes:
        """As a page: each line of the text a paragraph, verbatim. A
        "Homepage: example.com" line also links to that site, so identity
        resolution sees the profile name the company's domain."""
        paragraphs = []
        for line in filter(None, (raw.strip() for raw in self.text.splitlines())):
            m = _HOMEPAGE.match(line)
            if m:
                site = html.escape(m.group(1))
                paragraphs.append(f'<p>{html.escape(line[: m.start(1)])}'
                                  f'<a href="https://{site}/">{site}</a></p>')  # fmt: skip
            else:
                paragraphs.append(f"<p>{html.escape(line)}</p>")
        title = html.escape(self.title or "")
        return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
                f"</head><body>{''.join(paragraphs)}</body></html>").encode()  # fmt: skip


class SearchProvider(Protocol):
    """What the crawler needs from a search provider (LLD section 14)."""

    name: str

    async def search(
        self,
        query: str,
        num_results: int,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchResult]: ...

    async def aclose(self) -> None: ...


def normalize_results(raw: Iterable[tuple[str, str | None, str | None]]) -> list[SearchResult]:
    """http(s) URLs only, each once, positions renumbered in the provider's order."""
    out: list[SearchResult] = []
    seen: set[str] = set()
    for url, title, published in raw:
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append(SearchResult(url, title, published, len(out)))
    return out


def rank_candidates(hits: Iterable[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """Order candidate URLs from ``(url, query, position)`` hits, one entry per URL.

    A URL several queries return is more likely about the company than one
    only a single query found; ties go to the best position in any result
    list, then to discovery order. Each URL keeps the first query that found it.
    """
    first: dict[str, tuple[str, int]] = {}
    queries: dict[str, set[str]] = {}
    best: dict[str, int] = {}
    for order, (url, query, position) in enumerate(hits):
        first.setdefault(url, (query, order))
        queries.setdefault(url, set()).add(query)
        best[url] = min(best.get(url, position), position)
    ranked = sorted(first, key=lambda u: (-len(queries[u]), best[u], first[u][1]))
    return [(url, first[url][0]) for url in ranked]


# Competitor discovery queries (approved 2026-11-06), by family. A page these
# find still counts only on explicit evidence (extract/competitor.py); the
# category family feeds the candidate tier.
COMPETITOR_TEMPLATES: dict[str, tuple[str, ...]] = {
    "name": ('"{n}" competitors', '"{n}" alternatives', '"{n}" vs', '"{n}" versus',
             '"{n}" compared', '"{n}" comparison', 'switch from "{n}"', 'migrate from "{n}"',
             'apps like "{n}"', '"{n}" competitors {year}', '"{n}" review alternatives'),
    "product": ('"{n}" alternatives', '"{n}" vs'),
    "category": ("best {n} software", "top {n} tools", "{n} companies", "{n} vendors comparison"),
    "second_hop": ('"{r}" vs "{n}"', '"{r}" alternatives'),
}  # fmt: skip
# Other names the site uses for itself get only the first templates of "name".
_VARIANT_TEMPLATES = 2
MAX_CATEGORY_CHARS = 80


def _interleave(families: list[list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """Each family's first query before any family's second; one entry per query
    (case-insensitive), so a budget cut trims depth, not whole families."""
    out: dict[str, tuple[str, str]] = {}
    for i in range(max((len(f) for f in families), default=0)):
        for family in families:
            if i < len(family):
                out.setdefault(family[i][1].lower(), family[i])
    return list(out.values())


def competitor_queries(
    names: Iterable[str],
    products: Iterable[str] = (),
    categories: Iterable[str] = (),
    year: int | None = None,
) -> list[tuple[str, str]]:
    """``(template, query)`` for competitor discovery; ``names`` starts with the subject."""
    unique: dict[str, str] = {}
    for n in names:
        if n.strip():
            unique.setdefault(n.lower(), n)
    own = set(unique)
    year = year or date.today().year

    def family(key: str, value: str, templates: tuple[str, ...]) -> list[tuple[str, str]]:
        return [(f"{key}:{t}", t.format(n=value, year=year)) for t in templates]

    t = COMPETITOR_TEMPLATES
    families = [
        family("name", n, t["name"] if i == 0 else t["name"][:_VARIANT_TEMPLATES])
        for i, n in enumerate(unique.values())
    ]
    families += [family("product", p, t["product"]) for p in dict.fromkeys(products)
                 if p.strip() and p.lower() not in own]  # fmt: skip
    families += [family("category", c, t["category"]) for c in dict.fromkeys(categories)
                 if c.strip() and len(c) <= MAX_CATEGORY_CHARS]  # fmt: skip
    return _interleave(families)


def second_hop_queries(subject: str, rivals: Iterable[str]) -> list[tuple[str, str]]:
    """Queries about competitors already found, to find more and corroborate them."""
    return _interleave([
        [(f"second_hop:{t}", t.format(r=r, n=subject)) for t in COMPETITOR_TEMPLATES["second_hop"]]
        for r in dict.fromkeys(rivals)
    ])  # fmt: skip


class ExaSearch:
    name = "exa"

    def __init__(
        self,
        settings: SearchSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            timeout=settings.timeout,
            headers={
                "x-api-key": settings.api_key,
                "User-Agent": f"gtm-crawler/{__version__}",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        query: str,
        num_results: int,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchResult]:
        body: dict[str, object] = {"query": query, "numResults": num_results}
        if include_domains:
            body["includeDomains"] = list(include_domains)
        elif exclude_domains:  # an include list already limits the domains
            body["excludeDomains"] = list(exclude_domains)
        results = (await self._post(self.settings.endpoint, body)).get("results", [])
        out = normalize_results(
            (r["url"], r.get("title"), r.get("publishedDate"))
            for r in results
            if isinstance(r, dict) and isinstance(r.get("url"), str)
        )
        log.info("search.completed", extra={"provider": self.name, "query": query,
                                            "results": len(out)})  # fmt: skip
        return out

    async def contents(self, url: str) -> PageContents | None:
        """The provider's text of ``url`` (a page we may not fetch ourselves,
        e.g. a LinkedIn company page), or None if it has none."""
        endpoint = urlunsplit(urlsplit(self.settings.endpoint)._replace(path="/contents"))
        data = await self._post(endpoint, {"urls": [url], "text": True})
        for r in data.get("results", []):
            if isinstance(r, dict) and isinstance(r.get("text"), str) and r["text"].strip():
                title = r.get("title")
                return PageContents(r.get("url") or url, title if isinstance(title, str) else None,
                                    r["text"])  # fmt: skip
        return None

    async def _post(self, endpoint: str, body: dict[str, object]) -> dict[str, Any]:
        s = self.settings
        for attempt in range(s.max_retries + 1):
            last = attempt == s.max_retries
            try:
                response = await self._client.post(endpoint, json=body)
            except httpx.HTTPError as exc:
                if last:
                    raise SearchError(f"{type(exc).__name__}: {exc}") from exc
                await self._sleep(self._backoff(attempt))
                continue
            if response.status_code in _RETRY_STATUSES and not last:
                wait = retry_after_seconds(response.headers.get("retry-after"))
                if wait is None or wait <= s.max_retry_after:
                    await self._sleep(wait if wait is not None else self._backoff(attempt))
                    continue
            break
        if response.status_code != 200:
            # The body may echo the request; never log the key (it is only in a header).
            raise SearchError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SearchError("invalid JSON: not an object")
        return data

    def _backoff(self, attempt: int) -> float:
        base = self.settings.retry_backoff_seconds * 2.0**attempt
        return base * random.uniform(0.5, 1.0)  # noqa: S311 - jitter, not security


class FailoverSearch:
    """The configured providers in order, each behind its own circuit breaker."""

    def __init__(self, providers: list[SearchProvider], breaker: CircuitBreaker) -> None:
        self.providers = providers
        self.breaker = breaker

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()

    async def search(
        self,
        query: str,
        num_results: int,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> tuple[str, list[SearchResult]]:
        """(provider that answered, its results). Raises ``SearchError`` naming
        every provider's failure (or open circuit) if none answered."""
        failures: list[str] = []
        for provider in self.providers:
            wait = self.breaker.check(provider.name)
            if wait:
                failures.append(f"{provider.name}: circuit open ({wait:.0f}s left)")
                SEARCH_REQUESTS.labels(provider.name, "circuit_open").inc()
                continue
            try:
                results = await provider.search(
                    query, num_results, include_domains, exclude_domains
                )
            except SearchError as exc:
                self.breaker.record(provider.name, ok=False)
                SEARCH_REQUESTS.labels(provider.name, "error").inc()
                failures.append(f"{provider.name}: {exc}")
                log.warning("search.provider_failed",
                            extra={"provider": provider.name, "error": str(exc)})  # fmt: skip
                continue
            self.breaker.record(provider.name, ok=True)
            SEARCH_REQUESTS.labels(provider.name, "ok").inc()
            return provider.name, results
        raise SearchError("; ".join(failures) or "no search provider configured")

    async def contents(self, url: str) -> PageContents | None:
        """The first answering provider's text of ``url``. Raises ``SearchError``
        if none answered."""
        failures: list[str] = []
        for provider in self.providers:
            fetch = getattr(provider, "contents", None)
            if fetch is None:
                continue
            wait = self.breaker.check(provider.name)
            if wait:
                failures.append(f"{provider.name}: circuit open ({wait:.0f}s left)")
                continue
            try:
                found: PageContents | None = await fetch(url)
            except SearchError as exc:
                self.breaker.record(provider.name, ok=False)
                failures.append(f"{provider.name}: {exc}")
                continue
            self.breaker.record(provider.name, ok=True)
            return found
        raise SearchError("; ".join(failures) or "no provider serves page contents")


def make_search(
    settings: SearchSettings,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FailoverSearch | None:
    """Search over the providers with credentials; None if there are none."""
    providers: list[SearchProvider] = []
    if settings.api_key:
        providers.append(ExaSearch(settings, transport, sleep))
    if not providers:
        return None
    breaker = CircuitBreaker(
        "search", settings.circuit_failure_threshold, settings.circuit_cooldown_seconds,
        settings.circuit_cooldown_seconds * 8,
    )  # fmt: skip
    return FailoverSearch(providers, breaker)
