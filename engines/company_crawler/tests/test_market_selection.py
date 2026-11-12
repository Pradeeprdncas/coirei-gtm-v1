"""Source selection and budgets (market/select.py, engine._collect): targeted regression
scenarios on a scripted web. Each is a situation the live benchmark exposed or the
selection must handle; the search returns titles and dates, as Exa does.

``SCENARIO_METRICS`` collects, per scenario, what docs/MARKET_BENCHMARK.md reports:
questions answered, total found, useful and wasted sources.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from gtm_crawler.market import pdf
from gtm_crawler.market.engine import run_research
from gtm_crawler.market.models import (
    Budget,
    ProductInput,
    Report,
    ResearchMode,
    ResearchRequest,
    RetrievalStatus,
    SizingRequest,
)
from gtm_crawler.market.select import Candidate, Needs, evaluate, pick_wave
from gtm_crawler.search import SearchResult

from .helpers import response
from .pdfs import make_pdf, paragraph
from .test_market import TODAY, html, settings

SUBJECT = "EV charging infrastructure"
TOTAL = (
    "The European EV charging infrastructure market was valued at USD 13.8 billion in "
    "2024, according to our market outlook."
)
CONTEXT = (
    "Public EV charging infrastructure deployment continued across member states in "
    "2024, driven by the new regulation on alternative fuels."
)
needs_pdf = pytest.mark.skipif(not pdf.available()[0], reason="pip install -e .[pdf]")


def pdf_doc(*sentences: str, pages: int = 1) -> bytes:
    """Each sentence its own paragraph, word-wrapped, top to bottom; ``pages`` copies."""
    from .market_pdf_fixtures import _wrap

    items, y = [], 760.0
    for s in sentences:
        lines = _wrap(s)
        items += paragraph(56, y, lines)
        y -= 14 * len(lines) + 20
    return make_pdf([items] * pages)


class Web:
    """URL -> (body, content type), plus 403s."""

    def __init__(self, pages: dict[str, bytes | int]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return response(404)
        self.requested.append(url)
        body = self.pages.get(url)
        if isinstance(body, int):
            return response(body)
        if body is None:
            return response(404)
        kind = "application/pdf" if body.startswith(b"%PDF") else "text/html; charset=utf-8"
        return response(200, body, **{"content-type": kind})


class ScriptedSearch:
    """Results per query: the first rule whose substring is in the query; else ``default``."""

    def __init__(
        self,
        rules: list[tuple[str, list[tuple[str, str]]]],
        default: list[tuple[str, str]] | None = None,
    ) -> None:
        self.rules = rules
        self.default = default or []
        self.queries: list[str] = []

    async def search(
        self, query: str, num_results: int, include_domains: tuple[str, ...] = ()
    ) -> tuple[str, list[SearchResult]]:
        self.queries.append(query)
        hits = next((r for key, r in self.rules if key in query), self.default)
        return "scripted", [SearchResult(u, t, "2025-01-01", i) for i, (u, t) in enumerate(hits)]


SCENARIO_METRICS: dict[str, dict[str, Any]] = {}


def run(
    tmp_path: Path,
    web: Web,
    search: ScriptedSearch,
    budget: Budget,
    mode: ResearchMode = ResearchMode.INDUSTRY,
    **kw: Any,
) -> Report:
    req = ResearchRequest(
        mode=mode, subject=kw.pop("subject", SUBJECT), geography="Europe", budget=budget, **kw
    )
    return asyncio.run(
        run_research(
            req, settings(tmp_path), search=search, transport=httpx.MockTransport(web), today=TODAY
        )
    )


def record(name: str, report: Report) -> dict[str, Any]:
    by = {s.url: s for s in report.sources}
    useful = {e.source_id for e in report.evidence}
    tried = [s for s in by.values() if s.status is not RetrievalStatus.SKIPPED]
    total = report.sizing.results[0].base.tam if report.sizing and report.sizing.results else None
    cov = report.stats["coverage"]
    SCENARIO_METRICS[name] = {
        "questions": f"{cov['questions_answered']}/{cov['questions']}",
        "total": total,
        "useful_sources": sum(s.source_id in useful for s in tried),
        "wasted_attempts": sum(s.source_id not in useful for s in tried),
        "status": report.status.value,
        "stopped": report.stats["selection"]["stopped"],
    }
    return {"by": by, "total": total}


def sizing() -> SizingRequest:
    return SizingRequest(
        market_definition="EV charging infrastructure in Europe", year=2024, currency="USD"
    )


@needs_pdf
def test_market_estimate_page_beats_a_long_contextual_pdf(tmp_path: Path) -> None:
    """The EV case of runs 5-6: a tier-1 PDF without figures took the only slot."""
    web = Web(
        {
            "https://observatory.europa.eu/progress.pdf": pdf_doc(CONTEXT, pages=12),
            "https://advisory.example.com/europe-ev-charging-market-outlook": html(
                "Europe EV charging infrastructure market outlook 2030", TOTAL
            ),
        }
    )
    search = ScriptedSearch(
        [],
        [
            (
                "https://observatory.europa.eu/progress.pdf",
                "Charging infrastructure deployment report",
            ),
            (
                "https://advisory.example.com/europe-ev-charging-market-outlook",
                "Europe EV charging infrastructure market outlook 2030",
            ),
        ],
    )
    report = run(tmp_path, web, search, Budget(max_queries=2, max_sources=1), sizing=sizing())
    got = record("html estimate vs long contextual PDF", report)
    assert got["total"] == 13.8e9
    outlook = got["by"]["https://advisory.example.com/europe-ev-charging-market-outlook"]
    assert outlook.selection is not None and outlook.selection.wave == 1
    pdf_src = got["by"]["https://observatory.europa.eu/progress.pdf"]
    assert pdf_src.status is RetrievalStatus.SKIPPED and pdf_src.selection is not None
    assert pdf_src.status_detail.startswith("source budget")  # with its rationale kept


@needs_pdf
def test_official_pdf_with_the_only_estimate_is_collected(tmp_path: Path) -> None:
    web = Web(
        {
            "https://blog.example.com/tips": html(
                "EV charging tips",
                "Charging at home "
                "is cheaper for EV charging infrastructure users "
                "who plan ahead and compare tariffs.",
            ),
            "https://stats.europa.eu/ev-charging-market-2024.pdf": pdf_doc(TOTAL, CONTEXT),
        }
    )
    search = ScriptedSearch(
        [],
        [
            ("https://blog.example.com/tips", "EV charging tips for drivers"),
            (
                "https://stats.europa.eu/ev-charging-market-2024.pdf",
                "European EV charging infrastructure market statistics 2024",
            ),
        ],
    )
    report = run(
        tmp_path, web, search, Budget(max_queries=2, max_sources=2, max_pdfs=1), sizing=sizing()
    )
    got = record("official PDF with the only estimate", report)
    assert got["total"] == 13.8e9
    e = next(e for e in report.evidence if e.metrics and e.locator and e.locator.type == "pdf")
    assert e.locator.page == 1


@needs_pdf
def test_irrelevant_high_ranked_pdf_does_not_take_the_slot(tmp_path: Path) -> None:
    web = Web(
        {
            "https://city.gov/annual-report-2023.pdf": pdf_doc(
                "The municipal water utility served more households in 2023 than ever."
            ),
            "https://advisory.example.com/ev-market": html("Europe EV charging market", TOTAL),
        }
    )
    search = ScriptedSearch(
        [],
        [
            ("https://city.gov/annual-report-2023.pdf", "Annual report 2023 of the water utility"),
            ("https://advisory.example.com/ev-market", "Europe EV charging infrastructure market"),
        ],
    )
    report = run(tmp_path, web, search, Budget(max_queries=2, max_sources=1), sizing=sizing())
    got = record("irrelevant high-ranked PDF", report)
    assert got["total"] == 13.8e9
    water = got["by"]["https://city.gov/annual-report-2023.pdf"]
    assert water.status is RetrievalStatus.SKIPPED
    assert "names none of the subject's terms" in water.selection.reason


def test_duplicates_do_not_use_the_budget_and_an_alternative_is_tried(tmp_path: Path) -> None:
    same = html("Europe EV charging infrastructure market", TOTAL)
    web = Web(
        {
            "https://a.example.com/ev": same,
            "https://mirror.example.org/ev": same,
            "https://c.example.net/ev-demand": html(
                "EV charging demand",
                "Demand for EV "
                "charging infrastructure is driven by rising electric car sales in Europe.",
            ),
        }
    )
    search = ScriptedSearch(
        [],
        [
            ("https://a.example.com/ev", "Europe EV charging market size"),
            ("https://mirror.example.org/ev", "Europe EV charging market report"),
            ("https://c.example.net/ev-demand", "EV charging infrastructure demand"),
        ],
    )
    report = run(tmp_path, web, search, Budget(max_queries=3, max_sources=2), sizing=sizing())
    got = record("duplicate sources", report)
    by = got["by"]
    statuses = {u: s.status for u, s in by.items()}
    assert RetrievalStatus.DUPLICATE in statuses.values()
    assert statuses["https://c.example.net/ev-demand"] is RetrievalStatus.FETCHED
    assert report.stats["selection"]["used"]["useful"] == 2


@needs_pdf
def test_pdfs_compete_for_their_allowance_by_fit(tmp_path: Path) -> None:
    urls = {
        "https://x.europa.eu/europe-ev-charging-market-size-2024.pdf": (
            "Europe EV charging infrastructure market size 2024",
            pdf_doc(TOTAL),
        ),
        "https://y.europa.eu/notes.pdf": ("Meeting notes", pdf_doc(CONTEXT)),
        "https://z.europa.eu/annex.pdf": ("Annex III", pdf_doc(CONTEXT)),
    }
    web = Web({u: b for u, (_, b) in urls.items()})
    search = ScriptedSearch([], [(u, t) for u, (t, _) in urls.items()][::-1])  # best one last
    report = run(
        tmp_path, web, search, Budget(max_queries=2, max_sources=3, max_pdfs=1), sizing=sizing()
    )
    got = record("PDFs competing for a limited allowance", report)
    by = got["by"]
    assert (
        by["https://x.europa.eu/europe-ev-charging-market-size-2024.pdf"].status
        is RetrievalStatus.FETCHED
    )
    others = [by["https://y.europa.eu/notes.pdf"], by["https://z.europa.eu/annex.pdf"]]
    assert all(s.status is RetrievalStatus.SKIPPED for s in others)
    assert all("PDF allowance" in (s.status_detail or "") for s in others)
    assert got["total"] == 13.8e9


def test_a_failed_top_source_is_followed_by_an_alternative(tmp_path: Path) -> None:
    web = Web(
        {
            "https://research.example.com/europe-ev-charging-market": 403,
            "https://advisory.example.com/ev-outlook": html("Europe EV charging outlook", TOTAL),
        }
    )
    search = ScriptedSearch(
        [],
        [
            (
                "https://research.example.com/europe-ev-charging-market",
                "Europe EV charging infrastructure market size report 2025",
            ),
            (
                "https://advisory.example.com/ev-outlook",
                "EV charging infrastructure outlook Europe",
            ),
        ],
    )
    report = run(tmp_path, web, search, Budget(max_queries=2, max_sources=1), sizing=sizing())
    got = record("failed top source, then an alternative", report)
    assert (
        got["by"]["https://research.example.com/europe-ev-charging-market"].status
        is RetrievalStatus.FAILED
    )
    assert got["total"] == 13.8e9
    assert report.status.value == "partial"  # the failure is not hidden
    assert any("1 source(s) failed" in r for r in report.stats["status_reasons"])


def test_different_questions_get_different_sources(tmp_path: Path) -> None:
    """A page every query returns is picked once; then each open question gets its own."""
    overview = html(
        "EV charging infrastructure overview",
        "EV charging infrastructure "
        "is defined as the network of public and private charging points.",
    )
    pages = {
        "https://wiki.example.org/ev-charging": overview,
        "https://advisory.example.com/market": html("Europe EV charging market", TOTAL),
        "https://chargeco.example.com/pricing": html(
            "ChargeCo pricing",
            "EV charging infrastructure subscriptions cost $12 per month for small operators.",
        ),
        "https://law.example.eu/afir": html(
            "AFIR regulation",
            "The new EV charging "
            "infrastructure regulation requires chargers every 60 km on main roads.",
        ),
        "https://generic-a.example.info/a": html(
            "EV charging infrastructure blog",
            "EV charging infrastructure blog post about our team holiday and office.",
        ),
        "https://generic-b.example.biz/b": html(
            "EV charging infrastructure news",
            "EV charging infrastructure news about an award our company received.",
        ),
    }
    common = [
        ("https://wiki.example.org/ev-charging", "EV charging infrastructure overview"),
        ("https://generic-a.example.info/a", "EV charging infrastructure blog"),
        ("https://generic-b.example.biz/b", "EV charging infrastructure news"),
    ]
    search = ScriptedSearch(
        [
            (
                "market size",
                common
                + [
                    (
                        "https://advisory.example.com/market",
                        "Europe EV charging infrastructure market size 2024",
                    )
                ],
            ),
            (
                "pricing",
                common
                + [
                    (
                        "https://chargeco.example.com/pricing",
                        "EV charging infrastructure pricing plans",
                    )
                ],
            ),
            (
                "regulation",
                common
                + [("https://law.example.eu/afir", "EV charging infrastructure regulation Europe")],
            ),
        ],
        common,
    )
    report = run(
        tmp_path,
        Web(pages),
        search,
        Budget(max_queries=8, max_sources=4, max_pages_per_domain=3),
        sizing=sizing(),
    )
    got = record("different questions, different sources", report)
    fetched = {u for u, s in got["by"].items() if s.status is RetrievalStatus.FETCHED}
    assert {
        "https://advisory.example.com/market",
        "https://chargeco.example.com/pricing",
        "https://law.example.eu/afir",
    } <= fetched
    assert got["total"] == 13.8e9


def test_budget_exhaustion_is_partial_and_says_what_is_open(tmp_path: Path) -> None:
    pages = {
        f"https://site{i}.example.com/ev": html(f"EV charging infrastructure page {i}", CONTEXT)
        for i in range(6)
    }
    search = ScriptedSearch(
        [], [(u, "EV charging infrastructure deployment Europe") for u in pages]
    )
    report = run(
        tmp_path, Web(pages), search, Budget(max_queries=8, max_sources=1), sizing=sizing()
    )
    record("budget exhaustion", report)
    assert report.status.value == "partial"
    assert report.stats["selection"]["stopped"].startswith("source budget: 1 useful")
    open_ = [q for q in report.stats["questions"] if not q["answered"]]
    assert open_ and all(q["reason"] for q in open_)
    assert any("not collected: source budget" in q["reason"] for q in open_)
    assert any(i.stage == "budget" for i in report.issues)
    assert any(g.startswith("unanswered: ") and "(" in g for g in report.gaps)


def test_collection_stops_when_little_is_left_to_learn() -> None:
    """Every question answered by three domains and the total found: no value left."""
    from gtm_crawler.config import MarketSettings
    from gtm_crawler.market.plan import build_plan

    req = ResearchRequest(mode=ResearchMode.INDUSTRY, subject=SUBJECT)
    needs = Needs(build_plan(req, MarketSettings()))
    needs.domains = {q.key: {"a", "b", "c"} for q in needs.plan.questions}
    c = Candidate(
        url="https://x.example.com/ev",
        domain="example.com",
        tier=4,
        title="EV charging infrastructure market size",
        queries=["q"],
        questions=[q.key for q in needs.plan.questions],
    )
    sel = evaluate(c, needs, {"charging", "infrastructure"}, None, set(), TODAY)
    assert sel.value == 0 and sel.targets == []
    assert pick_wave([(c, sel)], needs, lambda c, chosen: True, 4) == []
    needs.domains["size"] = set()  # one question open again: worth collecting
    sel = evaluate(c, needs, {"charging", "infrastructure"}, None, set(), TODAY)
    assert sel.value > 0.5 and sel.targets == ["size"]


# --- found by the code review of the selection (each a defect before its fix) ---------


@needs_pdf
def test_a_wave_cannot_overrun_the_pdf_page_budget(tmp_path: Path) -> None:
    pdfs = {
        f"https://r{i}.europa.eu/ev-charging-market-{i}.pdf": pdf_doc(TOTAL, pages=4)
        for i in range(2)
    }
    search = ScriptedSearch(
        [], [(u, "Europe EV charging infrastructure market size") for u in pdfs]
    )
    report = run(
        tmp_path,
        Web(pdfs),
        search,
        Budget(max_queries=1, max_sources=3, max_pdfs=2, max_pdf_pages=3, max_extraction_pages=4),
        sizing=sizing(),
    )
    used = report.stats["selection"]["used"]
    assert used["pdfs"] == 2 and used["pdf_pages"] == 4  # 3 + 1, not 3 + 3


@needs_pdf
def test_a_pdf_behind_an_html_url_respects_the_pdf_allowance(tmp_path: Path) -> None:
    web = Web({"https://docs.example.eu/download?id=7": pdf_doc(TOTAL)})
    search = ScriptedSearch(
        [],
        [
            (
                "https://docs.example.eu/download?id=7",
                "Europe EV charging infrastructure market size",
            )
        ],
    )
    report = run(
        tmp_path, web, search, Budget(max_queries=1, max_sources=2, max_pdfs=0), sizing=sizing()
    )
    src = next(s for s in report.sources if s.url.startswith("https://docs.example.eu"))
    assert src.status is RetrievalStatus.SKIPPED and src.status_detail.startswith("PDF not read")
    assert report.stats["selection"]["used"]["pdfs"] == 1 and not report.evidence


def test_cancellation_stops_a_wave_and_competitor_pages_are_recorded(tmp_path: Path) -> None:
    class Slow(Web):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if "slow" in str(request.url):
                await asyncio.sleep(5)
            return await super().__call__(request)

    pages = {
        f"https://slow{i}.example.com/ev": html("EV charging infrastructure", TOTAL)
        for i in range(3)
    }
    search = ScriptedSearch([], [(u, "Europe EV charging infrastructure market") for u in pages])
    stop = asyncio.Event()

    async def go() -> Report:
        req = ResearchRequest(
            mode=ResearchMode.INDUSTRY, subject=SUBJECT, budget=Budget(max_queries=1, max_sources=3)
        )
        asyncio.get_running_loop().call_later(0.5, stop.set)
        return await run_research(
            req,
            settings(tmp_path),
            search=search,
            stop=stop,
            transport=httpx.MockTransport(Slow(pages)),
            today=TODAY,
        )

    started = asyncio.get_event_loop_policy().new_event_loop().time()
    report = asyncio.run(go())
    assert report.status.value == "cancelled"
    assert all(s.status_detail == "cancelled" for s in report.sources)
    assert asyncio.get_event_loop_policy().new_event_loop().time() - started < 4
    # Stopped before collection: competitor pages are recorded, not lost.
    stop2 = asyncio.Event()
    stop2.set()
    req = ResearchRequest(
        mode=ResearchMode.PRODUCT,
        subject="X",
        budget=Budget(max_queries=0),
        product=ProductInput(name="X", known_competitors=[]),
    )
    from gtm_crawler.market.models import Competitor

    req.product.known_competitors.append(Competitor(name="R", url="https://rival.example.com/"))
    got = asyncio.run(
        run_research(
            req,
            settings(tmp_path),
            search=ScriptedSearch([]),
            stop=stop2,
            transport=httpx.MockTransport(Web({})),
            today=TODAY,
        )
    )
    assert {s.url for s in got.sources} >= {"https://rival.example.com/"}
    assert all(s.status_detail == "cancelled" for s in got.sources)


def test_robots_refusals_are_not_attempts(tmp_path: Path) -> None:
    class Robots(Web):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://closed.example.com/robots.txt":
                return response(
                    200, b"User-agent: *\nDisallow: /\n", **{"content-type": "text/plain"}
                )
            return await super().__call__(request)

    pages = {"https://open.example.org/ev": html("Europe EV charging market", TOTAL)}
    search = ScriptedSearch(
        [],
        [
            ("https://closed.example.com/ev", "Europe EV charging market"),
            ("https://open.example.org/ev", "Europe EV charging market"),
        ],
    )
    report = run(
        tmp_path,
        Robots(pages),
        search,
        Budget(max_queries=1, max_sources=1, max_attempts=1),
        sizing=sizing(),
    )
    by = {s.url: s for s in report.sources}
    assert by["https://closed.example.com/ev"].status is RetrievalStatus.BLOCKED
    assert by["https://open.example.org/ev"].status is RetrievalStatus.FETCHED  # not blocked by it
    assert report.stats["selection"]["used"]["attempts"] == 1


def test_a_question_whose_searches_were_not_sent_says_so(tmp_path: Path) -> None:
    """Live (plant-based, run 7): 'no search result was aimed at it' hid that its query
    was over budget.max_queries."""
    pages = {"https://advisory.example.org/ev": html("Europe EV charging market", TOTAL)}
    search = ScriptedSearch([], [("https://advisory.example.org/ev", "Europe EV charging market")])
    report = run(tmp_path, Web(pages), search, Budget(max_queries=1, max_sources=2))
    reasons = {q["key"]: q["reason"] for q in report.stats["questions"] if not q["answered"]}
    assert reasons["business_models"] == (
        "its searches were not sent (budget.max_queries or deadline)"
    )


def test_scenario_metrics_were_recorded() -> None:
    """Printed for docs/MARKET_BENCHMARK.md (run with -s after the scenarios)."""
    for name, m in SCENARIO_METRICS.items():
        print(f"{name}: {m}")
