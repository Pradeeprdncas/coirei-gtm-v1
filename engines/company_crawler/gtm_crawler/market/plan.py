"""The research plan: the questions a run tries to answer and the searches for each.

Questions are listed in priority order and each names the dimensions that
would answer it, so the report can say which questions the evidence left
open. Queries beyond ``Budget.max_queries`` are recorded as not sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import MarketSettings
from .models import Dimension, ResearchMode, ResearchRequest

D = Dimension


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    dimensions: tuple[Dimension, ...]
    queries: tuple[str, ...]
    include_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Query:
    text: str
    question: str
    include_domains: tuple[str, ...] = ()


@dataclass
class Plan:
    questions: list[Question]
    queries: list[Query]
    over_budget: list[Query] = field(default_factory=list)


def _scope(req: ResearchRequest) -> str:
    return " ".join(p for p in (req.geography, req.segment) if p)


def industry_questions(req: ResearchRequest, market: MarketSettings) -> list[Question]:
    s, scope, year = req.subject, _scope(req), date.today().year
    return [
        Question("size", "How large is the market, and how fast is it growing?",
                 (D.MARKET_SIZE, D.GROWTH),
                 (f"{s} market size {scope}".strip(), f"{s} market forecast CAGR {scope}".strip())),
        Question("statistics", "What do official statistics say?", (D.MARKET_SIZE, D.SEGMENT,
                 D.ADOPTION), (f"{s} statistics {req.geography or ''}".strip(),),
                 market.statistics_domains),
        Question("definition", "What does the industry include and exclude?", (D.DEFINITION,),
                 (f"what is the {s} industry definition value chain",)),
        Question("demand", "What drives demand, and who buys?", (D.DEMAND_DRIVER, D.SEGMENT,
                 D.ADOPTION, D.PAIN_POINT), (f"{s} demand drivers customer segments {year}",)),
        Question("competition", "Who competes, and how concentrated is the market?",
                 (D.COMPETITION, D.PRICING),
                 (f"{s} leading companies market share {scope}".strip(),)),
        Question("external", "Which regulations and technology shifts matter?",
                 (D.REGULATION, D.TECHNOLOGY, D.TREND),
                 (f"{s} regulation {req.geography or ''}".strip(), f"{s} industry trends {year}")),
        Question("risks", "What are the barriers to entry and risks?", (D.BARRIER, D.RISK),
                 (f"{s} industry challenges barriers to entry",)),
        Question("business_models", "How do companies price and sell?", (D.PRICING,
                 D.DISTRIBUTION), (f"{s} business models pricing distribution channels",)),
    ]  # fmt: skip


def product_questions(req: ResearchRequest, market: MarketSettings) -> list[Question]:
    assert req.product is not None
    p = req.product
    category = req.industry or p.description or req.subject
    category = category if len(category) <= 80 else req.subject
    who = p.target_customer or req.segment or ""
    problem = p.problem or category
    return [
        Question("demand", "Is the problem real and acute, and for whom?", (D.PAIN_POINT,
                 D.DEMAND_DRIVER, D.ADOPTION), (f"{problem} {who} survey statistics".strip(),)),
        Question("alternatives", "What do buyers use today (competitors, substitutes)?",
                 (D.COMPETITION, D.SUBSTITUTE),
                 (f"{p.name} alternatives", f"best {category}" + (f" for {who}" if who else ""))),
        Question("size", "How large is the category, and how fast is it growing?",
                 (D.MARKET_SIZE, D.GROWTH),
                 (f"{category} market size {req.geography or ''}".strip(),)),
        Question("pricing", "What do buyers pay for comparable products?", (D.PRICING,
                 D.WILLINGNESS_TO_PAY), (f"{category} pricing per user per month",)),
        Question("workarounds", "How do buyers solve the problem without a product?",
                 (D.SUBSTITUTE, D.PAIN_POINT),
                 (f"{who} {problem} spreadsheet manual workaround".strip(),)),
        Question("go_to_market", "How do comparable products reach buyers?",
                 (D.DISTRIBUTION, D.SEGMENT), (f"{category} go-to-market channels {who}".strip(),)),
        Question("barriers", "What blocks adoption (regulation, cost, switching)?",
                 (D.BARRIER, D.REGULATION, D.RISK),
                 (f"{category} adoption barriers compliance {req.geography or ''}".strip(),)),
    ]  # fmt: skip


def build_plan(req: ResearchRequest, market: MarketSettings) -> Plan:
    questions = (industry_questions if req.mode is ResearchMode.INDUSTRY else product_questions)(
        req, market
    )
    # Round-robin over questions: each gets its first query before any its second.
    ordered: list[Query] = []
    depth = max(len(q.queries) for q in questions)
    for i in range(depth):
        for q in questions:
            if i < len(q.queries):
                ordered.append(Query(" ".join(q.queries[i].split()), q.key, q.include_domains))
    unique = list({q.text.lower(): q for q in ordered}.values())
    limit = req.limits.max_queries
    return Plan(questions, unique[:limit], unique[limit:])
