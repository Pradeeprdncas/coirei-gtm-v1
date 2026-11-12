"""Phase 9: search providers, retries, failover, budgets and stored queries."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, text

from gtm_crawler.breaker import CircuitBreaker
from gtm_crawler.config import SearchSettings, Secret
from gtm_crawler.db.models import SearchQuery, SearchResultRow, Url
from gtm_crawler.search import (
    COMPETITOR_TEMPLATES,
    ExaSearch,
    FailoverSearch,
    PageContents,
    SearchError,
    SearchResult,
    competitor_queries,
    make_search,
    normalize_results,
    second_hop_queries,
)

from .test_external import KEY, FakeSearch, external, run


def test_results_are_normalized() -> None:
    out = normalize_results([
        ("https://a.com/x", "A", None), (" https://b.com/ ", None, "2025-01-01"),
        ("https://a.com/x", "dup", None), ("javascript:alert(1)", None, None),
        ("ftp://c.com/f", None, None), ("HTTP://d.com/", None, None),
    ])  # fmt: skip
    assert out == [
        SearchResult("https://a.com/x", "A", None, 0),
        SearchResult("https://b.com/", None, "2025-01-01", 1),
        SearchResult("HTTP://d.com/", None, None, 2),
    ]


def test_competitor_queries_interleave_families() -> None:
    queries = [q for _, q in competitor_queries(
        ["Acme", "Acme Inc"], products=["Acme Flow", "acme"],
        categories=["CRM", "x" * 81], year=2026,
    )]  # fmt: skip
    # Each family's first query comes before any family's second.
    # The product named like the company adds nothing ("acme").
    assert queries[:5] == [
        '"Acme" competitors',
        '"Acme Inc" competitors',
        '"Acme Flow" alternatives',
        "best CRM software",
        '"Acme" alternatives',
    ]
    assert '"Acme" competitors 2026' in queries and "top CRM tools" in queries
    assert all("x" * 81 not in q for q in queries)  # over-long category skipped
    assert len(queries) == len({q.lower() for q in queries})
    # Only the first two templates for other names the site uses.
    assert sum("Acme Inc" in q for q in queries) == 2


def test_competitor_queries_dedupe_case_insensitively() -> None:
    queries = [q for _, q in competitor_queries(["Acme", "ACME"], products=["ACME"])]
    assert not any("ACME" in q for q in queries)  # same as the "Acme" queries


def test_second_hop_queries() -> None:
    assert [q for _, q in second_hop_queries("Acme", ["Globex", "Initech"])] == [
        '"Globex" vs "Acme"',
        '"Initech" vs "Acme"',
        '"Globex" alternatives',
        '"Initech" alternatives',
    ]


def test_excluded_domains_are_sent() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return OK

    provider = ExaSearch(SearchSettings(api_key=Secret(KEY)), httpx.MockTransport(handler))

    async def go() -> None:
        try:
            await provider.search("q", 5, exclude_domains=("acme.example",))
            await provider.search("q", 5)
        finally:
            await provider.aclose()

    asyncio.run(go())
    assert bodies[0]["excludeDomains"] == ["acme.example"]
    assert "excludeDomains" not in bodies[1]


class Script:
    """A search endpoint answering with a scripted list of responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


OK = httpx.Response(200, json={"results": [{"url": "https://a.com/", "title": "A"}]})


def exa(script: Script, **overrides: Any) -> tuple[ExaSearch, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    settings = replace(SearchSettings(api_key=Secret(KEY)), **overrides)
    return ExaSearch(settings, httpx.MockTransport(script), sleep), sleeps


def search(provider: ExaSearch) -> list[SearchResult]:
    async def go() -> list[SearchResult]:
        try:
            return await provider.search("q", 5)
        finally:
            await provider.aclose()

    return asyncio.run(go())


def test_transient_failures_are_retried_honouring_retry_after() -> None:
    script = Script(
        httpx.ConnectError("reset"),
        httpx.Response(429, headers={"Retry-After": "7"}),
        OK,
    )
    provider, sleeps = exa(script, retry_backoff_seconds=2.0)
    assert [r.url for r in search(provider)] == ["https://a.com/"]
    assert script.calls == 3
    assert 1.0 <= sleeps[0] <= 2.0 and sleeps[1] == 7.0  # jittered backoff, then Retry-After


def test_retries_are_bounded_and_long_retry_after_is_not_waited() -> None:
    script = Script(*(httpx.Response(503) for _ in range(3)))
    provider, _ = exa(script)
    with pytest.raises(SearchError, match="HTTP 503"):
        search(provider)
    assert script.calls == 3  # max_retries = 2

    script = Script(httpx.Response(429, headers={"Retry-After": "3600"}))
    provider, sleeps = exa(script)
    with pytest.raises(SearchError, match="HTTP 429"):
        search(provider)
    assert script.calls == 1 and sleeps == []


def test_client_errors_are_not_retried() -> None:
    script = Script(httpx.Response(401, text="bad key"))
    provider, _ = exa(script)
    with pytest.raises(SearchError, match="HTTP 401"):
        search(provider)
    assert script.calls == 1


class Fake:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    async def search(
        self,
        query: str,
        num_results: int,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchResult]:
        self.calls += 1
        if self.fail:
            raise SearchError("HTTP 500")
        return [SearchResult(f"https://{self.name}.example/", None, None)]

    async def aclose(self) -> None:
        pass


def test_contents_asks_the_provider_for_a_pages_text() -> None:
    bodies: list[tuple[str, dict[str, Any]]] = []
    texts = iter(["# Acme\n- Company Size: 51-200 employees", "  "])

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"results": [
            {"url": "https://www.linkedin.com/company/acme", "title": "Acme", "text": next(texts)}
        ]})  # fmt: skip

    provider = ExaSearch(SearchSettings(api_key=Secret(KEY)), httpx.MockTransport(handler))

    async def go() -> list[PageContents | None]:
        try:
            url = "https://www.linkedin.com/company/acme"
            return [await provider.contents(url), await provider.contents(url)]
        finally:
            await provider.aclose()

    first, blank = asyncio.run(go())
    assert bodies[0] == ("/contents", {"urls": ["https://www.linkedin.com/company/acme"],
                                       "text": True})  # fmt: skip
    assert first == PageContents("https://www.linkedin.com/company/acme", "Acme",
                                 "# Acme\n- Company Size: 51-200 employees")  # fmt: skip
    assert blank is None  # no text is no page


def test_contents_as_html_keep_each_line_verbatim_and_link_the_homepage() -> None:
    text = "# Acme\n\n- Homepage: acme.example\n- Note: <b>bold</b>"
    page = PageContents("https://www.linkedin.com/company/acme", "Acme <Inc>", text).to_html()
    assert b"<title>Acme &lt;Inc&gt;</title>" in page
    assert b"<p># Acme</p>" in page
    assert b'<p>- Homepage: <a href="https://acme.example/">acme.example</a></p>' in page
    assert b"<p>- Note: &lt;b&gt;bold&lt;/b&gt;</p>" in page


def test_failover_uses_the_next_provider_and_skips_open_circuits() -> None:
    primary, backup = Fake("primary", fail=True), Fake("backup")
    failover = FailoverSearch([primary, backup], CircuitBreaker("search", 2, 60, 600))

    async def go() -> list[tuple[str, list[SearchResult]]]:
        return [await failover.search("q", 5) for _ in range(4)]

    answers = asyncio.run(go())
    assert [provider for provider, _ in answers] == ["backup"] * 4
    assert primary.calls == 2  # then its circuit is open
    assert failover.breaker.state("primary") == "open"


def test_all_providers_failing_names_each() -> None:
    failover = FailoverSearch([Fake("a", fail=True), Fake("b", fail=True)],
                              CircuitBreaker("search", 5, 60, 600))  # fmt: skip
    with pytest.raises(SearchError, match=r"a: HTTP 500; b: HTTP 500"):
        asyncio.run(failover.search("q", 5))


def test_no_key_means_no_search() -> None:
    assert make_search(SearchSettings()) is None
    assert make_search(SearchSettings(api_key=Secret(KEY))) is not None


@pytest.mark.db
def test_queries_and_results_are_stored_without_the_key(engine: Engine, tmp_path: Path) -> None:
    summary, _ = run(engine, external(tmp_path), FakeSearch())
    with engine.connect() as conn:
        queries = conn.execute(
            select(SearchQuery).where(SearchQuery.crawl_id == summary.crawl_id)
            .order_by(SearchQuery.id)
        ).all()  # fmt: skip
        results = conn.execute(
            select(SearchResultRow.query_id, SearchResultRow.position, Url.url)
            .join(Url, Url.id == SearchResultRow.url_id)
            .order_by(SearchResultRow.query_id, SearchResultRow.position)
        ).all()  # fmt: skip
        dump = conn.execute(
            text("SELECT (SELECT json_agg(q) FROM search_query q)::text"
                 " || (SELECT json_agg(c.config) FROM crawl c)::text")
        ).scalar_one()  # fmt: skip
    assert [q.template for q in queries] == [
        '"{name}" funding', '"{name}" revenue customers', '"{name}"',
        *(f"name:{t}" for t in COMPETITOR_TEMPLATES["name"]),
    ]  # fmt: skip
    assert {q.status for q in queries} == {"ok"} and {q.provider for q in queries} == {"exa"}
    alternatives = queries[4]
    assert alternatives.query == '"Acme" alternatives' and alternatives.result_count == 4
    assert [r.url for r in results if r.query_id == alternatives.id] == [
        "https://reviews.example/acme-alternatives", "https://acme.example/",
        "https://news.example/a", "https://news.example/b",
    ]  # fmt: skip
    assert "linkedin.com" in json.dumps(queries[2].include_domains)
    assert KEY not in dump


@pytest.mark.db
def test_query_budget_is_enforced_and_recorded(engine: Engine, tmp_path: Path) -> None:
    # Gap queries only: competitor queries have their own budget (test_external.py).
    settings = external(tmp_path, max_competitor_queries=0)
    settings = replace(settings, search=replace(settings.search, max_queries_per_crawl=2))
    search = FakeSearch()
    summary, _ = run(engine, settings, search)
    assert len(search.requests) == 2
    assert summary.stats["external"]["queries_over_budget"] == 1
    with engine.connect() as conn:
        statuses = conn.execute(
            select(SearchQuery.status).where(SearchQuery.crawl_id == summary.crawl_id)
            .order_by(SearchQuery.id)
        ).scalars().all()  # fmt: skip
    assert statuses[:3] == ["ok", "ok", "over_budget"]
