"""Validated data model of market research (docs/MARKET_DATA_MODEL.md).

Requests, sources, evidence, metrics and the two report types. Everything a
report states points at ``EvidenceRecord`` ids, and every evidence record at
the ``SourceRecord`` it was quoted from. ``SCHEMA_VERSION`` is bumped when a
stored result's shape changes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 2: market scope on metrics and summaries, sizing exclusions; 3: competitor ads (source
# type ad_library, dimension messaging, locator type ad, ProductInput.include_ads)
SCHEMA_VERSION = "3"

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
LongText = Annotated[str, Field(max_length=4000)]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class ResearchMode(StrEnum):
    INDUSTRY = "industry"
    PRODUCT = "product"


class Depth(StrEnum):
    """How much to spend: scales the query and source budgets (``Budget.for_depth``)."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class ProductStage(StrEnum):
    CONCEPT = "concept"
    PROTOTYPE = "prototype"
    LAUNCHED = "launched"
    GROWTH = "growth"
    MATURE = "mature"


class Budget(Strict):
    """Hard limits for one research run (docs/MARKET_ENGINE_IMPLEMENTATION.md,
    "Source selection and budgets"). Open-web pages are fetched one at a time
    and their links are never followed (crawl depth 0, approved external-domain
    policy); competitors' own sites go through the crawler.

    ``max_sources`` counts *useful* search-result sources (fetched, relevant,
    not a duplicate, with at least one evidence record); failed, irrelevant,
    duplicate and empty ones count against ``max_attempts`` (default 2 x
    ``max_sources``), HTML pages downloaded against ``max_html_pages`` (default
    the attempt budget), PDFs against ``max_pdfs``, bytes against
    ``max_total_bytes``; PDF pages read against ``max_pdf_pages`` per document
    and ``max_extraction_pages`` per run."""

    max_queries: int = Field(8, ge=0, le=50)
    results_per_query: int = Field(10, ge=1, le=50)
    max_sources: int = Field(20, ge=0, le=200)
    max_attempts: int | None = Field(None, ge=0, le=400)
    max_html_pages: int | None = Field(None, ge=0, le=200)
    max_pdfs: int = Field(2, ge=0, le=50)
    max_pdf_pages: int = Field(50, ge=1, le=1000)
    max_extraction_pages: int = Field(150, ge=1, le=5000)
    max_total_bytes: int = Field(100_000_000, ge=100_000, le=2_000_000_000)
    max_pages_per_domain: int = Field(3, ge=1, le=50)
    request_timeout_seconds: float = Field(20.0, gt=0, le=120)
    max_content_bytes: int = Field(5_000_000, ge=10_000, le=50_000_000)
    max_duration_seconds: float = Field(600.0, gt=0, le=7200)
    max_llm_calls: int = Field(0, ge=0, le=50)

    @property
    def attempts(self) -> int:
        return 2 * self.max_sources if self.max_attempts is None else self.max_attempts

    @property
    def html_pages(self) -> int:
        return self.attempts if self.max_html_pages is None else self.max_html_pages

    @classmethod
    def for_depth(cls, depth: Depth) -> Budget:
        scale = {Depth.QUICK: (4, 8, 1), Depth.STANDARD: (8, 20, 2),
                 Depth.DEEP: (16, 50, 5)}[depth]  # fmt: skip
        return cls(max_queries=scale[0], max_sources=scale[1], max_pdfs=scale[2])


class Competitor(Strict):
    name: ShortText
    url: str | None = Field(None, max_length=2048)


class ProductInput(Strict):
    """What is known about the product; every field optional but the name."""

    name: ShortText
    description: LongText | None = None
    problem: LongText | None = None
    target_customer: str | None = Field(None, max_length=500)
    known_competitors: list[Competitor] = Field(default_factory=list, max_length=20)
    business_model: str | None = Field(None, max_length=200)
    price: str | None = Field(None, max_length=200)
    stage: ProductStage | None = None
    # Claimed differentiators: reported as *proposed*, never as observed.
    differentiators: list[ShortText] = Field(default_factory=list, max_length=20)
    # An already-crawled company (``POST /v1/crawls``) whose dossier and
    # competitors are reused as evidence.
    company_id: int | None = Field(None, ge=1)
    # The known competitors' captured ads (``gtm-market --import-ads``) as evidence,
    # with their landing pages fetched (market.ads).
    include_ads: bool = False


class ResearchRequest(Strict):
    research_id: str = Field(default_factory=lambda: new_id("mr"))
    mode: ResearchMode
    subject: ShortText
    description: LongText | None = None
    geography: str | None = Field(None, max_length=100)
    segment: str | None = Field(None, max_length=200)
    # Candidate segments to compare (opportunity scoring).
    segments: list[ShortText] = Field(default_factory=list, max_length=10)
    industry: str | None = Field(None, max_length=200)
    time_horizon_years: int = Field(5, ge=1, le=30)
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    assumptions: list[LongText] = Field(default_factory=list, max_length=50)
    depth: Depth = Depth.STANDARD
    budget: Budget | None = None
    product: ProductInput | None = None
    sizing: SizingRequest | None = None
    scoring: ScoringRequest | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> ResearchRequest:
        if self.mode is ResearchMode.PRODUCT and self.product is None:
            raise ValueError("product mode needs a 'product'")
        if self.budget is None:
            self.budget = Budget.for_depth(self.depth)
        return self

    @property
    def limits(self) -> Budget:
        assert self.budget is not None  # set by the validator
        return self.budget

    def subject_key(self) -> str:
        """Same subject, mode, industry, scope and product description -> same key (to
        reuse earlier evidence). The product part is a hash of every product field, so
        two products of the same name never share cached evidence."""
        parts = [self.mode, self.subject, self.industry or "", self.geography or "",
                 self.segment or ""]  # fmt: skip
        if self.product is not None:
            dump = json.dumps(self.product.model_dump(mode="json"), sort_keys=True)
            parts.append(hashlib.sha256(dump.encode()).hexdigest()[:16])
        return "|".join(p.strip().lower() for p in parts)


# --- sources and evidence ------------------------------------------------------------


class SourceType(StrEnum):
    GOVERNMENT = "government"
    REGULATORY = "regulatory_filing"
    BUSINESS_DATABASE = "business_database"
    MAJOR_PUBLICATION = "major_publication"
    INDUSTRY_PUBLICATION = "industry_publication"
    MARKET_RESEARCH = "market_research"
    COMPANY_WEBSITE = "company_website"
    PROFESSIONAL_PROFILE = "professional_profile"
    CRAWLED_DOSSIER = "crawled_dossier"
    UNVERIFIED = "unverified_third_party"
    # A captured ad (market.ads): proof of what an advertiser says, never that it is true.
    AD_LIBRARY = "ad_library"


class RetrievalStatus(StrEnum):
    FETCHED = "fetched"
    CACHED = "cached"  # reused from an earlier run of the same subject
    BLOCKED = "blocked"  # robots.txt, network guard
    FAILED = "failed"  # HTTP or network error
    IRRELEVANT = "irrelevant"  # fetched, but not about the subject
    DUPLICATE = "duplicate"  # same main text as an earlier source
    SKIPPED = "skipped"  # over a budget
    UNREADABLE = "unreadable"  # fetched, but the document cannot be read (encrypted PDF, ...)


class SourceRecord(Strict):
    source_id: str = Field(default_factory=lambda: new_id("src"))
    url: str
    final_url: str | None = None
    domain: str
    publisher: str | None = None
    source_type: SourceType = SourceType.UNVERIFIED
    tier: int = Field(4, ge=1, le=4)
    title: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    geographic_scope: str | None = None
    content_sha256: str | None = None
    raw_sha256: str | None = None
    # A PDF's extraction (page texts, tables, versions) as stored in the raw store.
    extracted_sha256: str | None = None
    status: RetrievalStatus
    status_detail: str | None = None
    discovered_by: str | None = None  # the query, or "known_competitor" / "dossier"
    extraction_method: str | None = None
    extraction_version: str | None = None
    # How an HTML page's text was built: "html-text-2" (inline boundaries spaced,
    # ParseSettings.inline_spacing), "html-text-3" (and landmark regions,
    # ParseSettings.landmark_regions); None: the parser's legacy text. Its quotes are
    # re-checked against the stored page parsed the same way.
    text_version: str | None = None
    quality: SourceQuality | None = None
    limitations: list[str] = Field(default_factory=list)
    size_bytes: int | None = None  # downloaded body size
    search_title: str | None = None  # what search said the page was
    selection: SourceSelection | None = None


class SourceSelection(Strict):
    """Why a search result was (or was not) collected (``market/select.py``):
    ``value = need_fit x quality``, judged before fetching it."""

    value: float
    need_fit: float
    quality: float
    components: dict[str, float] = Field(default_factory=dict)
    targets: list[str] = Field(default_factory=list)  # plan question keys it may answer
    reason: str
    wave: int | None = None  # the collection wave it was fetched in


class SourceQuality(Strict):
    """Per-source quality components, each 0-1 or None when it cannot be judged.

    ``score`` is the mean of the known components: an ordering aid between
    sources, not a probability that anything the source says is true."""

    authority: Fraction
    recency: Fraction | None = None
    methodology: Fraction | None = None
    geographic_relevance: Fraction | None = None
    specificity: Fraction | None = None
    score: Fraction
    notes: list[str] = Field(default_factory=list)


class Dimension(StrEnum):
    """What a piece of evidence speaks to."""

    MARKET_SIZE = "market_size"
    GROWTH = "growth"
    DEFINITION = "definition"
    DEMAND_DRIVER = "demand_driver"
    PAIN_POINT = "pain_point"
    ADOPTION = "adoption"
    WILLINGNESS_TO_PAY = "willingness_to_pay"
    SEGMENT = "segment"
    COMPETITION = "competition"
    PRICING = "pricing"
    SUBSTITUTE = "substitute"
    BARRIER = "barrier"
    REGULATION = "regulation"
    TECHNOLOGY = "technology"
    TREND = "trend"
    RISK = "risk"
    DISTRIBUTION = "distribution"
    # What a competitor's ad says. Ad text has only this dimension, so it never answers
    # a market question (demand, size, pain points) as if it were market evidence.
    MESSAGING = "messaging"


class EvidenceKind(StrEnum):
    DIRECT = "direct"  # quoted from a source
    DERIVED = "derived"  # computed from direct evidence
    INFERRED = "inferred"  # a rule's interpretation; labelled as such
    USER = "user"  # stated by the requester


class Locator(Strict):
    """Where the excerpt sits: an XPath with character offsets (re-checkable
    against the raw page with ``evidence.resolve``), a dossier field, an image
    (OCR), or a PDF page: ``page`` (one-based) with offsets into that page's
    normalized text (``pdf.PDF_TEXT_VERSION`` = ``version``), and for a table
    row its ``table`` and ``row`` (zero-based) on the page. A PDF quote is
    checked with ``verify.verify_evidence`` against the source's stored file
    (``raw_sha256``) and extracted text (``extracted_sha256``). An ``ad`` locator
    names one field of a stored ad capture: ``value`` "<field>", ``row`` the
    card's index for fields with one text per card (``market.ads``)."""

    type: str = Field(pattern=r"^(xpath|json_path|meta|dossier|image|pdf|ad)$")
    value: str
    char_start: int | None = None
    char_end: int | None = None
    page: int | None = Field(None, ge=1)
    version: str | None = None
    table: int | None = Field(None, ge=0)
    row: int | None = Field(None, ge=0)


class MetricKind(StrEnum):
    MARKET_SIZE = "market_size"  # value at a past or current year
    MARKET_FORECAST = "market_forecast"  # value at a future year
    GROWTH_RATE = "growth_rate"  # CAGR, percent per year
    PRICE_POINT = "price_point"  # an amount and its terms (``Metric.price``)
    # "Contact sales for pricing": the terms are known, the amount is not (value 0, unused).
    PRICE_ON_REQUEST = "price_on_request"
    SHARE = "share"  # a percentage of a population (adoption, share)


class PricingType(StrEnum):
    """What a price pays for (``market/pricing.py``). Per-user and per-seat prices are
    subscriptions with that ``basis``; percentage fees have ``percent``; tiers are plans."""

    SUBSCRIPTION = "subscription"  # a recurring charge for the product or a plan
    ONE_TIME = "one_time"  # a one-time purchase (a licence, a device, a report)
    SETUP = "setup"  # setup, implementation, onboarding, installation
    TRANSACTION = "transaction"  # per payment, order or sale, fixed or percentage
    USAGE = "usage"  # metered: per N API calls, GB, messages, kWh; overage
    UNIT = "unit"  # goods per unit of quantity (per lb, per kg, per item)
    ADD_ON = "add_on"  # an optional extra on top of a plan
    MINIMUM = "minimum"  # a minimum charge
    QUOTE = "quote"  # custom pricing / contact sales: no amount
    UNKNOWN = "unknown"  # stated as a price, but what it pays for is not


class PriceBound(StrEnum):
    EXACT = "exact"
    FROM = "from"  # "starts at", "from", "as low as": not every customer's price
    UP_TO = "up_to"  # "up to", "maximum"
    ABOUT = "about"  # "about", "around", "~"


class PriceContext(Strict):
    """Text outside the excerpt that a price's terms were read from (a pricing card's
    heading or the element after the amount, a table's row and column labels, a JSON-LD
    field), with its own locator so it can be re-checked like the excerpt."""

    role: str = Field(pattern=r"^(heading|card|row|column|json)$")
    text: str = Field(max_length=500)
    locator: Locator


class Attribution(StrEnum):
    """Where a price's plan and product names were read (``market/pricing.py``)."""

    CLAUSE = "clause"  # the price's own words ("The Pro plan is $29", "Pro: $29")
    HEADING = "heading"  # the heading above its card or paragraph
    TABLE = "table"  # its table row or column label
    STRUCTURED_DATA = "structured_data"  # its JSON-LD offer or product
    # The page names several plans and nothing ties this price to one: no plan is
    # guessed, the price is kept and said to be unattributed.
    UNCERTAIN = "uncertain"


class Price(Strict):
    """The terms of a price as the source states them; nothing is converted or inferred
    (no currency conversion, no period when none is stated, no annualizing)."""

    pricing_type: PricingType
    currency: str | None  # ISO 4217; None: a percentage, or a "$" the page does not settle
    currency_assumed: bool = False  # read from a bare symbol ("$" as USD)
    percent: bool = False  # the value is a percentage ("2.9% per transaction")
    period: str | None = None  # month, year, week, day, hour, quarter: as stated
    billed: str | None = None  # monthly, annually, quarterly: a stated billing commitment
    basis: str | None = None  # per what besides time: user, seat, transaction, lb, API calls
    basis_quantity: float | None = None  # "per 1,000 API calls" = 1000
    bound: PriceBound = PriceBound.EXACT
    plan: str | None = None  # the plan, tier or package
    product: str | None = None  # the product priced, when named apart from the vendor
    vendor: str | None = None  # the known competitor whose own page states it
    # Where plan / product came from; None: neither is named (and none is needed).
    attribution: Attribution | None = None
    promotional: bool = False
    tax: str | None = None  # included / excluded, when stated
    included: str | None = None  # a stated allowance ("500 contacts")
    effective: str | None = None  # a date the source says the price applies from or as of
    context: list[PriceContext] = Field(default_factory=list)
    # What a reader must know before using it ("no billing period stated", ...).
    ambiguities: list[str] = Field(default_factory=list)


class ScopeBasis(StrEnum):
    STATED = "stated"  # the figure's own clause names it
    CARRIED = "carried"  # from the previous figure of the same sentence ("... and is expected to")
    NOT_STATED = "not_stated"


class Metric(Strict):
    """A number stated in an excerpt, normalized; the excerpt stays the authority.

    Market figures (sizes, forecasts, growth rates) carry their scope as the
    sentence states it (``market/scope.py``): never taken from the page title
    or another sentence, so two figures of one page can describe different
    markets."""

    kind: MetricKind
    value: float
    # A currency code, "percent" or "percent_per_year"; a price: its currency (or
    # "percent", "unknown") / basis / period, e.g. "USD/user/month" (terms in ``price``).
    unit: str
    year: int | None = None
    period_start: int | None = None
    period_end: int | None = None
    text: str
    geography: str | None = None  # "Global", "North America", "India", ...; None = not stated
    segment: str | None = None  # "SMB", "Cloud", ...; None = the whole market (or not stated)
    measure: str | None = None  # "market", "spending" or "revenue"
    market_phrase: str | None = None  # the market as the sentence names it
    off_subject: bool = False  # names a market none of whose words is a subject term
    # Words of the market phrase beyond the subject's vocabulary ("marketing services"):
    # possibly a narrower or different market than the subject.
    qualifier: str | None = None
    subset: bool = False  # stated as part of a larger figure, without a scope of its own
    scope_basis: ScopeBasis = ScopeBasis.NOT_STATED
    price: Price | None = None  # PRICE_POINT and PRICE_ON_REQUEST


class EvidenceRecord(Strict):
    evidence_id: str = Field(default_factory=lambda: new_id("ev"))
    source_id: str
    claim: str = Field(max_length=2000)
    excerpt: str = Field(max_length=2000)
    locator: Locator | None = None
    dimensions: list[Dimension] = Field(min_length=1)
    kind: EvidenceKind = EvidenceKind.DIRECT
    metrics: list[Metric] = Field(default_factory=list)
    period: str | None = None
    geography: str | None = None
    segment: str | None = None
    # A rule's certainty that the excerpt says what ``dimensions`` claim (0-1);
    # see docs/MARKET_ENGINE_IMPLEMENTATION.md, "Evidence quality".
    extraction_certainty: Fraction = 0.6
    contradicted_by: list[str] = Field(default_factory=list)
    # Which run collected it, when reused from an earlier one.
    cached_from: str | None = None


# --- assessment ---------------------------------------------------------------------


class Confidence(StrEnum):
    """Claim confidence, by rule (not a calibrated probability):

    HIGH: two or more independent domains agree, best source quality >= 0.7,
    no unresolved contradiction. MEDIUM: one source of quality >= 0.6, or two
    independent ones. LOW: anything else that has evidence. INSUFFICIENT: none.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class Contradiction(Strict):
    topic: str
    # The metric compared: kind, unit and year (the period's end for growth rates).
    metric: MetricKind | None = None
    unit: str | None = None
    year: int | None = None
    geography: str | None = None
    segment: str | None = None
    measure: str | None = None
    qualifier: str | None = None
    evidence_ids: list[str] = Field(min_length=2)
    values: list[str]
    explanation: str
    possible_causes: list[str] = Field(default_factory=list)


class Finding(Strict):
    """One statement of the report and the evidence behind it."""

    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Confidence
    basis: EvidenceKind = EvidenceKind.DIRECT
    independent_domains: int = 0


class Section(Strict):
    title: str
    findings: list[Finding] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.findings


class MetricSummary(Strict):
    """One metric = one scope: kind, unit, year, geography, segment, measure, and
    whether the figures are parts of a larger one or name another market."""

    kind: MetricKind
    unit: str
    year: int | None
    geography: str | None = None  # None = not stated
    segment: str | None = None
    measure: str | None = None
    subset: bool = False
    off_subject: bool = False
    qualifier: str | None = None
    low: float
    high: float
    evidence_ids: list[str]
    independent_domains: int
    confidence: Confidence
    contradicted: bool
    # One source states several different values for this scope: the figures
    # may describe different markets the rules cannot tell apart.
    ambiguous: bool = False
    notes: list[str] = Field(default_factory=list)


# --- market sizing ------------------------------------------------------------------


class Basis(StrEnum):
    SOURCED = "sourced"  # an evidence record states it
    ASSUMPTION = "assumption"  # an analyst's or user's assumption
    DERIVED = "derived"  # calculated here


class Quantity(Strict):
    """An input with its range: ``low <= base <= high``."""

    name: ShortText
    base: float = Field(ge=0)
    low: float | None = Field(None, ge=0)
    high: float | None = Field(None, ge=0)
    unit: str = Field(min_length=1, max_length=40)
    basis: Basis
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str | None = Field(None, max_length=1000)
    # Filters only: what the fraction narrows (geography, segment, ...); two
    # filters on the same dimension would double-count.
    dimension: str | None = Field(None, max_length=60)

    @model_validator(mode="after")
    def _range(self) -> Quantity:
        lo = self.base if self.low is None else self.low
        hi = self.base if self.high is None else self.high
        if not lo <= self.base <= hi:
            raise ValueError(f"{self.name}: need low <= base <= high")
        if self.basis is Basis.SOURCED and not self.evidence_ids:
            raise ValueError(f"{self.name}: a sourced input needs evidence_ids")
        if self.basis is Basis.ASSUMPTION and not self.rationale:
            raise ValueError(f"{self.name}: an assumption needs a rationale")
        return self

    @property
    def lo(self) -> float:
        return self.base if self.low is None else self.low

    @property
    def hi(self) -> float:
        return self.base if self.high is None else self.high


class TopDownInput(Strict):
    """TAM = market_total; SAM = TAM x filters; SOM = SAM x obtainable_share."""

    market_total: Quantity
    sam_filters: list[Quantity] = Field(default_factory=list, max_length=10)
    obtainable_share: Quantity | None = None


class BottomUpInput(Strict):
    """TAM = customers x units_per_customer x price_per_unit (per year);
    SAM = TAM x filters; SOM = SAM x penetration."""

    customers: Quantity
    units_per_customer: Quantity
    price_per_unit: Quantity
    sam_filters: list[Quantity] = Field(default_factory=list, max_length=10)
    penetration: Quantity | None = None


class SizingRequest(Strict):
    market_definition: LongText
    geography: str | None = Field(None, max_length=100)
    segment: str | None = Field(None, max_length=200)
    year: int = Field(default_factory=lambda: date.today().year, ge=1990, le=2100)
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    top_down: TopDownInput | None = None
    bottom_up: BottomUpInput | None = None
    # Use the best market-size estimate found in the evidence as the top-down
    # total when ``top_down`` is not given (marked unverified unless corroborated).
    use_evidence_total: bool = True


class Scenario(Strict):
    tam: float
    sam: float | None
    som: float | None


class SensitivityRow(Strict):
    input: str
    som_at_low: float | None
    som_at_high: float | None
    swing: float


class SizingResult(Strict):
    method: str  # "top_down" or "bottom_up"
    currency: str
    year: int
    geography: str | None
    segment: str | None
    formula: list[str]
    inputs: list[Quantity]
    low: Scenario
    base: Scenario
    high: Scenario
    sensitivity: list[SensitivityRow]
    verified: bool
    warnings: list[str] = Field(default_factory=list)


class ExcludedFigure(Strict):
    """A collected market-size figure not used as the top-down total, and why."""

    figure: str
    evidence_ids: list[str]
    reason: str


class SizingReport(Strict):
    market_definition: str
    results: list[SizingResult] = Field(default_factory=list)
    cross_check: str | None = None
    insufficient: list[str] = Field(default_factory=list)
    # The scope collected figures had to match to be the top-down total.
    target_scope: str | None = None
    excluded: list[ExcludedFigure] = Field(default_factory=list)


# --- opportunity scoring ------------------------------------------------------------


class ScoringRequest(Strict):
    """Weights per dimension (normalized to sum 1); ratings the user supplies
    override derived ones and are labelled as user input."""

    weights: dict[str, float] | None = None
    ratings: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("weights")
    @classmethod
    def _positive(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is not None and (not v or any(w < 0 for w in v.values()) or sum(v.values()) <= 0):
            raise ValueError("weights must be non-negative and not all zero")
        return v

    @field_validator("ratings")
    @classmethod
    def _scale(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for ratings in v.values():
            if any(not 1 <= r <= 5 for r in ratings.values()):
                raise ValueError("ratings are on a 1-5 scale")
        return v


class DimensionScore(Strict):
    dimension: str
    rating: float | None  # 1-5, None when unknown
    weight: float
    contribution: float | None
    basis: str  # "evidence", "user" or "unknown"
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class OpportunityScore(Strict):
    candidate: str
    score: float | None  # 1-5 after the coverage penalty; None if nothing is known
    raw_score: float | None
    coverage: Fraction  # share of the weight with a rating
    penalty: float
    dimensions: list[DimensionScore]
    rank: int | None = None


class ScoringReport(Strict):
    config_id: str  # hash of method version and weights; compare only equal ids
    weights: dict[str, float]
    candidates: list[OpportunityScore]
    sensitivity: list[str] = Field(default_factory=list)
    interpretation: str


# --- run results --------------------------------------------------------------------


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"  # finished, but a source, provider or budget limited it
    FAILED = "failed"
    CANCELLED = "cancelled"


class Issue(Strict):
    stage: str
    severity: str = Field(pattern=r"^(info|warning|error)$")
    message: str


class Report(Strict):
    """The shared report shape; ``sections`` are keyed by mode-specific names."""

    schema_version: str = SCHEMA_VERSION
    analysis_version: str
    research_id: str
    mode: ResearchMode
    subject: str
    generated_at: datetime = Field(default_factory=utcnow)
    request: ResearchRequest
    status: RunStatus
    executive_summary: list[Finding]
    sections: dict[str, Section]
    metrics: list[MetricSummary] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    sizing: SizingReport | None = None
    scoring: ScoringReport | None = None
    assumptions: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    def evidence_index(self) -> dict[str, EvidenceRecord]:
        return {e.evidence_id: e for e in self.evidence}


ResearchRequest.model_rebuild()
SourceRecord.model_rebuild()
