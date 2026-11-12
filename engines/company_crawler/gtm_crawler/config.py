"""Configuration loading.

Settings are read from a TOML file and individual keys can be overridden by
environment variables of the form ``GTM__<SECTION>__<KEY>`` (for example
``GTM__DATABASE__URL``). The double underscore keeps keys that themselves
contain underscores unambiguous.

Unknown sections or keys are rejected rather than ignored: a misspelled limit
silently falling back to its default is exactly the kind of error that makes a
crawler misbehave without anyone noticing.

Profiles: ``GTM_ENV`` (``dev`` by default, ``test``, ``staging`` or ``prod``)
selects an optional overlay file next to the base file, ``config.<env>.toml``,
whose values override the base file; environment variables override both. The
``staging`` and ``prod`` profiles fail closed with the same checks: they refuse
to start without the settings a safe deployment needs (``_check_prod``), so
staging proves a prod-shaped configuration.

Credentials are ``Secret`` values: usable as strings, but shown as ``***`` in
``repr`` and registered with the log redactor, and never written to crawl
configuration snapshots.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

ENV_PREFIX = "GTM__"
CONFIG_PATH_ENV = "GTM_CONFIG"
PROFILE_ENV = "GTM_ENV"
PROFILES = ("dev", "test", "staging", "prod")
# Profiles held to the production safety checks (staging mirrors prod).
FAIL_CLOSED_PROFILES = frozenset({"staging", "prod"})
DEFAULT_CONFIG_PATH = Path("config.toml")
DEV_DATABASE_URL = "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler"
# Experimental logic behind [features].enabled (spec section 31). A flag must
# be listed here before it can be turned on, so a typo cannot pass silently.
KNOWN_FEATURES: frozenset[str] = frozenset()


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed or invalid."""


class Secret(str):
    """A credential: a normal string to code that uses it, ``***`` everywhere else."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Secret('***')" if self else "Secret('')"


@dataclass(frozen=True)
class AppSettings:
    # Set by GTM_ENV; selects config.<env>.toml and (staging, prod) the safety checks.
    env: str = "dev"

    def __post_init__(self) -> None:
        if self.env not in PROFILES:
            raise ConfigError(f"app.env: expected one of {', '.join(PROFILES)}, got {self.env!r}")


@dataclass(frozen=True)
class FeatureSettings:
    # Names of experimental features to turn on; each must be in KNOWN_FEATURES.
    enabled: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = sorted(set(self.enabled) - KNOWN_FEATURES)
        if unknown:
            raise ConfigError(f"features.enabled: unknown feature flag(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class DatabaseSettings:
    url: Secret = Secret(DEV_DATABASE_URL)
    # Pool per process; size it against the process's concurrency (spec 21, 30).
    pool_size: int = 10
    max_overflow: int = 5
    pool_timeout: float = 30.0
    # Bound on opening a connection, so probes and workers fail fast when the
    # database is unreachable instead of hanging.
    connect_timeout_seconds: int = 5
    # Server-side limits on every statement and lock wait (0 disables).
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 10_000

    def __post_init__(self) -> None:
        _secrets(self)
        _require_positive(
            self, "database",
            allow_zero=("max_overflow", "statement_timeout_ms", "lock_timeout_ms"),
        )  # fmt: skip


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    # Share of new traces whose span records are logged (tracing.py); 0 = none.
    trace_sample_rate: float = 1.0

    def __post_init__(self) -> None:
        if self.level.upper() not in logging.getLevelNamesMapping():
            raise ConfigError(f"logging.level: unknown level {self.level!r}")
        if not 0.0 <= self.trace_sample_rate <= 1.0:
            raise ConfigError("logging.trace_sample_rate: must be between 0 and 1")


@dataclass(frozen=True)
class UrlSettings:
    # Query (and ;path) parameters removed during normalization: tracking and
    # session identifiers that never change page content. fnmatch patterns,
    # matched case-insensitively.
    strip_params: tuple[str, ...] = (
        "utm_*", "gclid", "dclid", "fbclid", "msclkid", "yclid", "igshid",
        "mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "mkt_tok",
        "jsessionid", "phpsessid", "aspsessionid", "sessionid", "cfid", "cftoken",
    )  # fmt: skip
    # Directory index files that address the same resource as the directory.
    index_files: tuple[str, ...] = ("index.html", "index.htm")
    # Off by default: /about and /about/ can legitimately be different resources.
    strip_trailing_slash: bool = False


@dataclass(frozen=True)
class FrontierSettings:
    max_depth: int = 8
    # Refused URLs (traps, budgets) stored per crawl with their reason; the
    # rest are only counted (crawl stats frontier_rejections).
    max_recorded_rejections: int = 2_000
    max_queue_size: int = 50_000
    max_url_length: int = 2048
    max_path_segments: int = 12
    max_query_params: int = 8
    # A path segment repeated more than this many times indicates a relative-link loop.
    max_segment_repeats: int = 3
    # URLs sharing one generalized shape (/events/{n}/{n}, /search?q) — calendars, facets.
    max_urls_per_pattern: int = 50
    # URLs under one host + first path segment, so /blog cannot consume the crawl.
    max_urls_per_prefix: int = 500
    # Per-domain URL budget (spec section 7): URLs admitted per host.
    max_urls_per_host: int = 5_000
    # URLs with a query string per path, whatever the parameters: sort/filter
    # combinations, faceted navigation, endless query variants.
    max_query_urls_per_path: int = 30
    # Low unique-content yield: once this many pages of one URL shape were
    # fetched, stop the shape if fewer than min_unique_yield of them brought new
    # content (duplicates, redirects to known pages, empty pages): template
    # duplication, pagination past the end, session-id copies.
    yield_min_fetches: int = 10
    min_unique_yield: float = 0.2

    def __post_init__(self) -> None:
        _require_positive(self, "frontier", allow_zero=("max_depth", "min_unique_yield"))
        if self.min_unique_yield > 1:
            raise ConfigError("frontier.min_unique_yield: must be between 0 and 1")


@dataclass(frozen=True)
class IdentitySettings:
    product_token: str = "GTMCrawler"  # noqa: S105 - user-agent product token, not a secret
    # URL or email where site operators can reach whoever runs the crawler.
    # Required before any fetch; there is deliberately no default.
    user_agent_contact: str = ""


@dataclass(frozen=True)
class FetchSettings:
    # TCP connect plus TLS handshake.
    connect_timeout: float = 10.0
    # From sending the request to receiving the response headers (connect included).
    ttfb_timeout: float = 30.0
    # Longest wait for any single read or write once connected.
    read_timeout: float = 20.0
    write_timeout: float = 20.0
    # Wall-clock cap per attempt (request + body), so a slow-drip server cannot hold a worker.
    attempt_timeout: float = 60.0
    max_redirects: int = 5
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    # A Retry-After longer than this gives up instead of waiting.
    max_retry_after: float = 120.0
    # Applies to both the transferred and the decompressed size.
    max_body_bytes: int = 10 * 1024 * 1024
    # A body that decompresses to more than this many times its transferred
    # size is a decompression bomb (checked once it passes ..._min_bytes).
    max_decompression_ratio: float = 100.0
    decompression_ratio_min_bytes: int = 1024 * 1024
    allowed_ports: tuple[int, ...] = (80, 443)
    # Networks allowed although not public (spec 9: explicit deployment
    # policy), e.g. ("10.20.0.0/16",) for an internal test site. Empty = public
    # addresses only. Cloud metadata endpoints are refused regardless.
    allowed_networks: tuple[str, ...] = ()
    # DNS answers are cached this long, for at most this many hosts.
    dns_cache_seconds: float = 300.0
    dns_cache_size: int = 1024
    # Send If-None-Match / If-Modified-Since from an earlier crawl's fetch of
    # the same URL; a 304 reuses that stored (hash-verified) body.
    conditional_requests: bool = True
    # Negotiate HTTP/2 where servers offer it. Needs the optional `h2`
    # package: pip install ".[http2]".
    http2: bool = False

    def __post_init__(self) -> None:
        _require_positive(
            self, "fetch", allow_zero=("max_redirects", "max_retries", "dns_cache_size")
        )
        for network in self.allowed_networks:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ConfigError(f"fetch.allowed_networks: {exc}") from exc
        if self.http2 and importlib.util.find_spec("h2") is None:
            raise ConfigError('fetch.http2 needs the h2 package: pip install ".[http2]"')


@dataclass(frozen=True)
class RobotsSettings:
    # RFC 9309 requires parsing at least the first 500 KiB.
    max_bytes: int = 512 * 1024
    # RFC 9309 section 2.4: cached rules should not be used for more than 24 hours.
    max_age_seconds: float = 86_400.0
    # A Crawl-delay longer than this is not shortened: the origin is deferred
    # instead (one request per such delay cannot finish within the crawl budgets).
    max_crawl_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        _require_positive(self, "robots")


@dataclass(frozen=True)
class SitemapSettings:
    max_sitemaps: int = 50
    max_urls: int = 50_000
    # Nesting depth of sitemap indexes to follow.
    max_index_depth: int = 3

    def __post_init__(self) -> None:
        _require_positive(self, "sitemaps")


@dataclass(frozen=True)
class ParseSettings:
    # Bounds per page so a pathological document cannot dominate a crawl.
    max_links: int = 5_000
    max_blocks: int = 20_000
    max_microdata_items: int = 200
    # Characters of surrounding block text kept with each link (a priority signal).
    link_context_chars: int = 200
    # A page whose parse takes longer is given up (URL failed, "parse_timeout").
    timeout_seconds: float = 30.0
    # A space where adjacent inline elements read as separate words ("EmailOctopus" +
    # "Pro"), not where they continue one ("$" + "29"); parse/dom.py. Off: the text
    # (and the XPath offsets of stored evidence) of PARSER_VERSION 3. Market research
    # sets it per source (SourceRecord.text_version).
    inline_spacing: bool = True
    # The text of an aria-hidden inline element (a layout "Placeholder" badge, a "|"
    # separator, an icon) is not read; a hidden block (a closed modal, an inactive
    # slide) still is. Off: PARSER_VERSION 3 text and market text versions.
    skip_aria_hidden: bool = True
    # Inside the main landmark (<main>, role=main) only landmark elements and roles make
    # a region: there a <header>/<footer> heads a section and a class name ("widget",
    # "cs-sidebar-enabled") is no landmark. Off: PARSER_VERSION 3 regions.
    landmark_regions: bool = False

    def __post_init__(self) -> None:
        _require_positive(self, "parse")


@dataclass(frozen=True)
class RawStoreSettings:
    # "filesystem" (under root) or "s3" (any S3-compatible service).
    backend: str = "filesystem"
    root: str = "data/raw"
    # Retention (approved 2026-09-29): documents that evidence cites and
    # robots.txt snapshots are kept forever; others are pruned by
    # `gtm-crawl --prune` after this many days.
    retention_days: int = 90
    # S3 backend. Credentials: the standard AWS chain (AWS_ACCESS_KEY_ID, ...).
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_endpoint_url: str = ""  # for non-AWS services, e.g. http://127.0.0.1:8333
    s3_region: str = ""

    def __post_init__(self) -> None:
        _require_positive(self, "rawstore")
        if self.backend not in ("filesystem", "s3"):
            raise ConfigError(f"rawstore.backend: expected filesystem or s3, got {self.backend!r}")
        if self.backend == "s3" and not self.s3_bucket:
            raise ConfigError("rawstore.s3_bucket is required when rawstore.backend = 's3'")


@dataclass(frozen=True)
class CrawlSettings:
    max_pages: int = 1_000
    max_duration_seconds: int = 3_600
    concurrency: int = 8
    per_host_concurrency: int = 2
    # While the site is crawled, assess what was found so far every this many parsed
    # pages, so the profile fills in progressively (0: only after the site).
    progress_every_pages: int = 50
    # Minimum gap between requests to one host; robots.txt Crawl-delay raises it.
    min_delay_seconds: float = 0.5
    # Stop early if this share of fetches fail (after error_rate_min_fetches fetches).
    max_error_rate: float = 0.5
    error_rate_min_fetches: int = 20
    # SimHash Hamming distance at or below which two pages count as near-duplicates.
    near_duplicate_bits: int = 3
    # Stop (partial) after downloading this many response bytes.
    max_bytes: int = 500 * 1024 * 1024
    # A URL is claimed under a lease; if its worker dies the lease expires and
    # the URL is claimed again. Longer than one URL's fetch plus render.
    lease_seconds: int = 600
    # Attempts per URL for retryable failures (timeouts, 429, 5xx), each after
    # the fetcher's own quick retries; later attempts wait retry_delay_seconds
    # (or the server's Retry-After, if longer).
    max_url_attempts: int = 2
    retry_delay_seconds: float = 60.0
    # How often the crawl saves its in-memory state for resumption.
    checkpoint_seconds: float = 10.0
    # Adaptive per-host rate control (throttle.py): a response slower than this
    # lowers the host's concurrency; this many fast responses in a row raise it
    # again; 429/503 add an extra delay between requests, up to the maximum.
    slow_response_seconds: float = 10.0
    rate_recover_after: int = 10
    max_extra_delay_seconds: float = 60.0
    # Per-host circuit breaker (breaker.py): this many failures in a row
    # (timeouts, connection errors, 5xx, 429) stop requests to the host for the
    # cooldown; each failed probe doubles it, up to the maximum.
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 60.0
    circuit_max_cooldown_seconds: float = 900.0
    # Profiles and directories: recorded as references, never followed during the
    # site crawl. The external phase may fetch one page of each, subject to robots.txt.
    reference_domains: tuple[str, ...] = (
        "linkedin.com", "crunchbase.com", "g2.com", "capterra.com", "x.com", "twitter.com",
        "youtube.com", "github.com", "producthunt.com", "facebook.com", "instagram.com",
        "glassdoor.com", "pitchbook.com", "angel.co", "wellfound.com",
        # Company directories and databases (Business Intelligence Profile).
        "theorg.com", "tracxn.com", "zaubacorp.com", "clutch.co", "ambitionbox.com",
    )  # fmt: skip
    # Retry policy by error class (spec section 23; fetch.http.retry_class):
    # "rate_limited" (429, 503) waits the server's Retry-After, else this long;
    # "dns" (resolution failures, often an outage) this long; everything else
    # retryable ("transient": timeouts, connection errors, 5xx) retry_delay_seconds.
    # No class waits less than retry_delay_seconds.
    retry_delay_rate_limited_seconds: float = 300.0
    retry_delay_dns_seconds: float = 300.0
    # A URL whose lease ran out this many times (its worker died holding it,
    # e.g. a page that crashes the process) goes to the dead-letter queue.
    max_url_lease_expiries: int = 3

    def __post_init__(self) -> None:
        _require_positive(
            self, "crawl",
            allow_zero=("min_delay_seconds", "near_duplicate_bits", "retry_delay_seconds",
                        "checkpoint_seconds"),
        )  # fmt: skip
        if self.max_error_rate > 1:
            raise ConfigError("crawl.max_error_rate: must be between 0 and 1")


@dataclass(frozen=True)
class SourceSettings:
    """Third-party domains by source tier (first-party pages are always tier 1).

    Matched against the host and its parent domains. Anything unlisted is
    tier 4, unverified third party.
    """

    # Tier 1: official records kept by regulators and registries.
    regulatory_domains: tuple[str, ...] = (
        "sec.gov", "find-and-update.company-information.service.gov.uk",
        "companieshouse.gov.uk", "sedarplus.ca", "handelsregister.de", "europa.eu",
    )  # fmt: skip
    # Tier 2.
    business_database_domains: tuple[str, ...] = (
        "crunchbase.com", "pitchbook.com", "cbinsights.com", "dnb.com",
    )  # fmt: skip
    major_publication_domains: tuple[str, ...] = (
        "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "cnbc.com", "forbes.com",
        "businessinsider.com", "economist.com", "nytimes.com", "techcrunch.com",
        "fortune.com", "axios.com",
    )  # fmt: skip
    # Tier 3.
    industry_publication_domains: tuple[str, ...] = ()
    professional_profile_domains: tuple[str, ...] = ("linkedin.com", "github.com")


@dataclass(frozen=True)
class SearchSettings:
    """External search (Exa). Disabled while ``api_key`` is empty.

    Set the key through the environment (``GTM__SEARCH__API_KEY``) rather
    than a config file; it is never stored with crawl records.
    """

    api_key: Secret = Secret("")
    endpoint: str = "https://api.exa.ai/search"
    timeout: float = 20.0
    results_per_query: int = 10
    # Retries per query for network errors, 429 and 5xx (jittered backoff, or
    # the provider's Retry-After when it is at most max_retry_after seconds).
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    max_retry_after: float = 30.0
    # Gap-query budget per crawl (spec section 14); further planned queries are
    # skipped. Competitor queries have their own (external.max_competitor_queries).
    max_queries_per_crawl: int = 10
    # A provider failing this many queries in a row is skipped for the cooldown.
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 300.0

    def __post_init__(self) -> None:
        _secrets(self)
        _require_positive(self, "search", allow_zero=("max_retries",))


@dataclass(frozen=True)
class ExternalSettings:
    """What may be fetched outside the company's own site (approved policy).

    Never the open web: only search results for the company (competitor and
    alternative pages), and single profile pages on reference domains that the
    company's site links to or that search returns for this exact company.
    External pages' links are recorded, never followed.
    """

    enabled: bool = True
    max_search_pages: int = 20
    max_reference_pages: int = 10
    # Searches sent, and external pages fetched, at once (each host stays throttled).
    concurrency: int = 4
    # Competitor discovery (approved 2026-11-06), its own budget beside the
    # gap queries': queries sent and result pages fetched per crawl, and how
    # many known competitors the second round asks about (0 turns it off).
    max_competitor_queries: int = 30
    max_competitor_search_pages: int = 60
    second_hop_competitors: int = 5
    # Competitors' own sites, crawled for competitor analysis once their
    # domain is known; home page and high-value pages first.
    max_competitor_crawls: int = 5
    competitor_max_pages: int = 30

    def __post_init__(self) -> None:
        _require_positive(
            self, "external",
            allow_zero=("max_search_pages", "max_reference_pages", "max_competitor_crawls",
                        "max_competitor_queries", "max_competitor_search_pages",
                        "second_hop_competitors"),
        )  # fmt: skip


@dataclass(frozen=True)
class BrowserSettings:
    """Headless Chromium fallback for pages whose HTML carries no usable content."""

    enabled: bool = True
    # Render when the HTTP page has less cleaned text than this and runs scripts.
    min_text_chars: int = 250
    max_renders: int = 100
    concurrency: int = 2
    # Wall-clock cap per render, including waiting for the page to settle.
    render_timeout: float = 30.0
    # How long to wait for network idle after the DOM is ready; then take what is there.
    settle_timeout: float = 5.0
    # Per-render resource budgets: requests the page may make, and bytes they
    # may bring in; further requests are refused (the render keeps what it has).
    max_requests_per_render: int = 100
    max_bytes_per_render: int = 20 * 1024 * 1024
    # Chromium is replaced after this many renders (leaks do not accumulate)
    # and relaunched after a crash, at most max_restarts times per crawl.
    recycle_after_renders: int = 50
    max_restarts: int = 3
    # V8 heap cap per renderer process, in MiB (memory-pressure protection).
    js_heap_mb: int = 512
    # This many failed renders in a row stop rendering for the cooldown
    # (breaker.CircuitBreaker); pages are then used as fetched.
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 300.0
    # Chromium's own OS sandbox (namespaces + seccomp-bpf on Linux). The
    # container needs deploy/chrome-seccomp.json for it (docs/security-review.md).
    # staging/prod refuse to render without it unless allow_unsandboxed says so.
    sandbox: bool = True
    allow_unsandboxed: bool = False

    def __post_init__(self) -> None:
        _require_positive(
            self, "browser", allow_zero=("max_renders", "settle_timeout", "max_restarts")
        )


@dataclass(frozen=True)
class PrioritySettings:
    """URL scoring. Terms are matched against URL path tokens and link text.

    These lists steer the order of the crawl; they never restrict which
    in-site URLs may be crawled.
    """

    high_value_terms: tuple[str, ...] = (
        "pricing", "price", "plans", "product", "products", "platform", "features",
        "solutions", "customers", "case-studies", "case-study", "about", "company",
        "team", "leadership", "investors", "investor-relations", "press", "newsroom",
        "news", "partners", "integrations", "contact", "locations", "offices", "funding",
        "compare", "vs", "alternative", "alternatives", "imprint", "impressum",
    )  # fmt: skip
    medium_value_terms: tuple[str, ...] = (
        "docs", "documentation", "blog", "resources", "stories", "changelog",
        "release-notes", "updates", "careers", "jobs", "legal", "terms", "security",
        "enterprise", "use-cases", "industries",
    )  # fmt: skip
    low_value_terms: tuple[str, ...] = (
        "tag", "tags", "category", "categories", "author", "login", "signin", "sign-in",
        "signup", "sign-up", "register", "cart", "checkout", "search", "feed", "rss",
        "print", "share", "wp-json", "cdn-cgi", "calendar", "cookie", "cookies",
    )  # fmt: skip
    path_weight: float = 4.0
    anchor_weight: float = 3.0
    context_weight: float = 1.0
    sitemap_weight: float = 1.0
    depth_weight: float = 2.0
    inlink_weight: float = 0.5
    low_value_penalty: float = 4.0
    # Links found on a page whose title/description is about a GTM topic.
    parent_weight: float = 1.0
    # Recently modified pages (sitemap <lastmod>) first; no credit past the horizon.
    freshness_weight: float = 1.0
    freshness_horizon_days: int = 730

    def __post_init__(self) -> None:
        _require_positive(self, "priority")


@dataclass(frozen=True)
class AssessmentSettings:
    # Field-assessment thresholds (approved 2026-09-29). Any other values are
    # recorded in each assessment's method_version. verified_confidence is the
    # CORROBORATED threshold, found_confidence the SUPPORTED one (fa-v2 names
    # the states as spec section 19 does; the setting names are kept).
    verified_confidence: float = 0.80
    found_confidence: float = 0.60
    # A time-varying figure whose newest evidence is older than this is STALE.
    stale_after_days: int = 730

    def __post_init__(self) -> None:
        _require_positive(self, "assessment")
        if not self.found_confidence <= self.verified_confidence <= 1:
            raise ConfigError("assessment: need found_confidence <= verified_confidence <= 1")


@dataclass(frozen=True)
class WorkerSettings:
    # A worker holds a job under a lease renewed every lease_seconds / 3; if
    # it dies, another worker resumes the job once the lease expires.
    lease_seconds: float = 60.0
    poll_seconds: float = 2.0
    # A job that keeps killing its workers is failed after this many claims.
    max_job_attempts: int = 5
    # Prometheus /metrics for the worker; port 0 disables it. Loopback by
    # default; set 0.0.0.0 in a container so the scraper can reach it.
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9100

    def __post_init__(self) -> None:
        _require_positive(self, "worker", allow_zero=("metrics_port",))


@dataclass(frozen=True)
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    # When set, every request except the probes and /metrics needs
    # "Authorization: Bearer <token>". Set it through GTM__API__TOKEN.
    token: Secret = Secret("")
    # Rotation: the token being retired, still accepted alongside ``token``
    # until every client has switched; then unset it (docs/staging.md).
    previous_token: Secret = Secret("")
    # Backpressure (spec section 23): new jobs are refused (429, Retry-After)
    # while this many are already waiting for a worker.
    max_queued_jobs: int = 1_000
    # /v1 requests accepted per client address per minute (429 beyond it).
    rate_limit_per_minute: int = 600
    # Serve Swagger UI at /docs (HTML that loads scripts from a CDN). Local
    # development only: staging/prod refuse to start with it on.
    docs: bool = False
    # Largest company document accepted at onboarding (POST .../documents).
    max_document_bytes: int = 10_000_000
    # Browser origins allowed to call the API (CORS), e.g. a deployed frontend.
    # Empty: no CORS headers (a dev frontend reaches the API through its own proxy).
    cors_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _secrets(self)
        _require_positive(self, "api")
        if self.previous_token and not self.token:
            raise ConfigError("api.previous_token needs api.token (else the API is open)")


@dataclass(frozen=True)
class CostSettings:
    """Unit prices in USD for cost accounting (spec section 27; ``cost.py``).

    0 means the price is unknown: usage is still recorded, its cost is 0.
    Byte quantities are priced per GB (10^9 bytes). ``search_query_usd``
    lists ``"provider=price"`` per query, e.g. ``["exa=0.005"]``.
    """

    http_request_usd: float = 0.0
    http_gb_usd: float = 0.0
    browser_render_usd: float = 0.0
    browser_second_usd: float = 0.0
    browser_gb_usd: float = 0.0
    search_query_usd: tuple[str, ...] = ()
    # Raw artifacts, charged once per GB stored (e.g. monthly price x retention months).
    storage_gb_usd: float = 0.0
    database_second_usd: float = 0.0
    worker_second_usd: float = 0.0

    def __post_init__(self) -> None:
        _require_positive(self, "cost", allow_zero=tuple(f.name for f in fields(self)))
        self.search_prices()

    def search_prices(self) -> dict[str, float]:
        """``search_query_usd`` as provider -> USD per query."""
        out = {}
        for item in self.search_query_usd:
            provider, _, price = (p.strip() for p in item.partition("="))
            try:
                value = float(price)
            except ValueError:
                value = -1.0
            if not provider or not 0 <= value < float("inf"):
                raise ConfigError(f"cost.search_query_usd: expected 'provider=price', got {item!r}")
            out[provider] = value
        return out


@dataclass(frozen=True)
class MarketSettings:
    """Market research (``gtm_crawler.market``): source classes and optional extras.

    Open-web pages are classified by these lists before the crawler's own
    ``sources`` lists. Being listed describes what kind of publisher a domain
    is; it never makes a statement true.
    """

    # Tier 1 "government": hosts ending in these suffixes, or under these domains.
    government_suffixes: tuple[str, ...] = (
        ".gov", ".gov.uk", ".gov.in", ".gov.au", ".gc.ca", ".gouv.fr", ".europa.eu", ".int",
        ".mil", ".gob.mx", ".go.jp",
    )  # fmt: skip
    statistics_domains: tuple[str, ...] = (
        "oecd.org", "worldbank.org", "imf.org", "un.org", "census.gov", "bls.gov",
        "ons.gov.uk", "eurostat.ec.europa.eu", "mospi.gov.in",
    )  # fmt: skip
    # Tier 3 "market_research": commercial research publishers. Their headline
    # figures are usually summaries of paywalled reports with undisclosed methods.
    market_research_domains: tuple[str, ...] = (
        "grandviewresearch.com", "marketsandmarkets.com", "fortunebusinessinsights.com",
        "precedenceresearch.com", "mordorintelligence.com", "statista.com", "gartner.com",
        "idc.com", "forrester.com", "imarcgroup.com", "alliedmarketresearch.com",
        "researchandmarkets.com", "globenewswire.com", "prnewswire.com",
    )  # fmt: skip
    # Runs waiting for a worker before POST /v1/market/research answers 429.
    max_queued_runs: int = 100
    # OCR of images on fetched pages (needs Tesseract; ``pip install -e .[ocr]``).
    ocr_enabled: bool = False
    tesseract_cmd: str = ""
    ocr_max_images_per_page: int = 4
    ocr_max_image_bytes: int = 5_000_000
    # Text PDFs (``pip install -e .[pdf]``), read in a child process (market/pdf.py):
    # refused above pdf_max_bytes, read up to pdf_max_pages / pdf_max_chars, killed after
    # pdf_timeout seconds; pdf_memory_mb caps its memory on POSIX (not on Windows).
    pdf_enabled: bool = True
    pdf_max_bytes: int = 20_000_000
    pdf_max_pages: int = 300
    pdf_max_chars: int = 3_000_000
    pdf_timeout: float = 60.0
    pdf_memory_mb: int = 1024
    # Optional narrative over collected evidence through an OpenAI-compatible
    # chat endpoint. Off while ``llm_model`` is empty. Facts, numbers and
    # scores never come from it (docs/MARKET_ENGINE_IMPLEMENTATION.md).
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: Secret = Secret("")
    llm_timeout: float = 60.0
    llm_max_evidence: int = 40

    def __post_init__(self) -> None:
        _secrets(self)
        _require_positive(self, "market")
        if self.llm_model:
            # The key and the evidence go there: plain http only to this machine.
            url = urlsplit(self.llm_base_url)
            if not url.hostname or not (
                url.scheme == "https"
                or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"))
            ):
                raise ConfigError("market.llm_base_url: https, or http on localhost only")


@dataclass(frozen=True)
class LlmSettings:
    """Page labels and company facts from an OpenAI-compatible chat endpoint
    (``extract/llm.py``): Ollama (``http://localhost:11434/v1``, ``qwen3:8b``),
    vLLM, Groq, Gemini. Off while ``model`` is empty. Every fact it gives must
    quote the page verbatim, or it is dropped."""

    base_url: str = ""
    model: str = ""
    api_key: Secret = Secret("")
    timeout: float = 120.0
    # Own pages labelled per extraction (home page first); the rest keep rules only.
    max_pages: int = 200
    # Page text sent per call, in characters.
    max_chars: int = 4000
    max_tokens: int = 600
    concurrency: int = 4
    # Sent as ``reasoning_effort``: "none" stops a thinking model (Qwen3 on
    # Ollama) spending the token budget on a reasoning trace. Empty: not sent,
    # for endpoints that reject the parameter.
    reasoning_effort: str = "none"
    # The company profile (profile.py): evidence sent, in characters, and the answer's
    # budget, which a thinking model's reasoning also spends. The profile reasons
    # ("medium"): it infers the business model and value proposition. Ollama's
    # default 4096-token context is too small: set OLLAMA_CONTEXT_LENGTH=16384.
    profile_max_chars: int = 14000
    profile_max_tokens: int = 8000
    profile_reasoning_effort: str = "medium"
    # A reasoning call on a laptop GPU takes two to four minutes.
    profile_timeout: float = 600.0

    def __post_init__(self) -> None:
        _secrets(self)
        _require_positive(self, "llm")
        if self.model:
            # The key and page text go there: plain http only to this machine.
            url = urlsplit(self.base_url)
            if not url.hostname or not (
                url.scheme == "https"
                or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"))
            ):
                raise ConfigError("llm.base_url: https, or http on localhost only")


@dataclass(frozen=True)
class Settings:
    app: AppSettings = field(default_factory=AppSettings)
    features: FeatureSettings = field(default_factory=FeatureSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    urls: UrlSettings = field(default_factory=UrlSettings)
    frontier: FrontierSettings = field(default_factory=FrontierSettings)
    identity: IdentitySettings = field(default_factory=IdentitySettings)
    fetch: FetchSettings = field(default_factory=FetchSettings)
    robots: RobotsSettings = field(default_factory=RobotsSettings)
    sitemaps: SitemapSettings = field(default_factory=SitemapSettings)
    parse: ParseSettings = field(default_factory=ParseSettings)
    rawstore: RawStoreSettings = field(default_factory=RawStoreSettings)
    crawl: CrawlSettings = field(default_factory=CrawlSettings)
    priority: PrioritySettings = field(default_factory=PrioritySettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    sources: SourceSettings = field(default_factory=SourceSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    external: ExternalSettings = field(default_factory=ExternalSettings)
    assessment: AssessmentSettings = field(default_factory=AssessmentSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    cost: CostSettings = field(default_factory=CostSettings)
    market: MarketSettings = field(default_factory=MarketSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)

    def __post_init__(self) -> None:
        if self.app.env in FAIL_CLOSED_PROFILES:
            _check_prod(self)

    def feature(self, name: str) -> bool:
        return name in self.features.enabled


def _check_prod(s: Settings) -> None:
    """Fail closed: refuse a staging/production start that would be unsafe or unaccountable."""
    problems = []
    if not s.identity.user_agent_contact.strip():
        problems.append("identity.user_agent_contact is required")
    if not s.api.token:
        problems.append("api.token is required (set GTM__API__TOKEN)")
    if s.api.docs:
        problems.append("api.docs must be off (the API serves no HTML)")
    if s.database.url == DEV_DATABASE_URL:
        problems.append("database.url is the development default")
    if s.database.statement_timeout_ms == 0:
        problems.append("database.statement_timeout_ms must be set")
    if s.logging.level.upper() == "DEBUG":
        problems.append("logging.level DEBUG is not allowed (it may log page content)")
    b = s.browser
    if b.enabled and not b.sandbox and not b.allow_unsandboxed:
        problems.append("browser.sandbox is off (set browser.allow_unsandboxed to accept that)")
    if problems:
        raise ConfigError(f"{s.app.env} profile: " + "; ".join(problems))


def secret_values(settings: Settings) -> list[str]:
    """Every non-empty Secret in ``settings`` (for log redaction)."""
    out = []
    for section in fields(settings):
        value = getattr(settings, section.name)
        for f in fields(value):
            item = getattr(value, f.name)
            if isinstance(item, Secret) and item:
                out.append(str(item))
    return out


def _secrets(obj: Any) -> None:
    """Wrap credential fields in Secret however the settings were built (file, env, code)."""
    for f in fields(obj):
        if f.type in (Secret, "Secret") and not isinstance(getattr(obj, f.name), Secret):
            object.__setattr__(obj, f.name, Secret(getattr(obj, f.name)))


def _require_positive(obj: Any, section: str, allow_zero: tuple[str, ...] = ()) -> None:
    for f in fields(obj):
        value = getattr(obj, f.name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        if value < 0 or (value == 0 and f.name not in allow_zero):
            raise ConfigError(f"{section}.{f.name}: must be positive")


def _find_root_dotenv() -> Path | None:
    """Walk up from this file to find the project root .env (the one next to app/)."""
    here = Path(__file__).resolve().parent
    for directory in [here, *here.parents]:
        candidate = directory / ".env"
        if candidate.exists() and (directory / "app").is_dir():
            return candidate
    return None


def _load_root_dotenv(env: dict[str, str]) -> None:
    """Load vars from the project root .env into env (does not overwrite existing vars)."""
    dotenv_path = _find_root_dotenv()
    if dotenv_path is None:
        return
    try:
        with dotenv_path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip optional surrounding quotes.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if key and key not in env:
                    env[key] = value
    except OSError:
        pass

    # Map shared GROQ_* keys to GTM-specific overrides when not already set.
    for src, dst in (
        ("GROQ_API_KEY", "GTM__LLM__API_KEY"),
        ("GROQ_BASE_URL", "GTM__LLM__BASE_URL"),
        ("GROQ_MODEL", "GTM__LLM__MODEL"),
    ):
        if src in env and dst not in env:
            env[dst] = env[src]


def load_settings(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings from ``path`` (or ``$GTM_CONFIG`` / ``config.toml``) plus env overrides.

    A missing default ``config.toml`` is fine (defaults apply); a missing file
    that was named explicitly is an error.
    """
    # Build a mutable copy so we can inject root .env values without mutating os.environ.
    env: dict[str, str] = dict(os.environ if environ is None else environ)
    _load_root_dotenv(env)
    explicit = path is not None or CONFIG_PATH_ENV in env
    config_path = path or Path(env.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))

    data: dict[str, dict[str, Any]] = {}
    if config_path.exists():
        data = _read_toml(config_path)
    elif explicit:
        raise ConfigError(f"Config file not found: {config_path}")

    profile = env.get(PROFILE_ENV)
    if profile is not None:
        if profile not in PROFILES:
            raise ConfigError(f"{PROFILE_ENV}: expected one of {', '.join(PROFILES)}")
        overlay = config_path.with_name(f"{config_path.stem}.{profile}{config_path.suffix}")
        if overlay.exists():
            for section, values in _read_toml(overlay).items():
                if not isinstance(values, dict):
                    raise ConfigError(f"{overlay}: {section}: expected a table")
                data.setdefault(section, {}).update(values)
        data.setdefault("app", {})["env"] = profile

    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = key[len(ENV_PREFIX) :].lower().split("__")
        if len(parts) != 2:
            raise ConfigError(f"{key}: expected {ENV_PREFIX}<SECTION>__<KEY>")
        section, name = parts
        data.setdefault(section, {})[name] = _EnvValue(raw)

    settings: Settings = _build(Settings, data, "")
    return settings


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc


class _EnvValue(str):
    """Marks a value that came from the environment and still needs coercion."""


def _build(cls: type[Any], data: Mapping[str, Any], prefix: str) -> Any:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{prefix.rstrip('.')}: expected a table")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"Unknown config key(s): {', '.join(prefix + k for k in sorted(unknown))}"
        )

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        typ = hints[f.name]
        value = data[f.name]
        if isinstance(typ, type) and hasattr(typ, "__dataclass_fields__"):
            kwargs[f.name] = _build(typ, value, f"{prefix}{f.name}.")
        else:
            kwargs[f.name] = _coerce(value, typ, prefix + f.name)
    return cls(**kwargs)


def _coerce(value: Any, typ: Any, name: str) -> Any:
    if get_origin(typ) is tuple:  # homogeneous tuple[T, ...]
        item_type = get_args(typ)[0]
        if isinstance(value, _EnvValue):
            items = [_EnvValue(i.strip()) for i in value.split(",") if i.strip()]
        elif isinstance(value, list):
            items = value
        else:
            raise ConfigError(f"{name}: expected a list of {item_type.__name__}")
        return tuple(_coerce(item, item_type, name) for item in items)

    if typ is Secret:
        if not isinstance(value, str):
            raise ConfigError(f"{name}: expected a string")
        return Secret(value)

    if isinstance(value, _EnvValue):
        text = str(value)
        if typ is bool:
            if text.lower() in {"1", "true", "yes"}:
                return True
            if text.lower() in {"0", "false", "no"}:
                return False
            raise ConfigError(f"{name}: expected a boolean, got {text!r}")
        try:
            return typ(text)
        except ValueError as exc:
            raise ConfigError(f"{name}: expected {typ.__name__}, got {text!r}") from exc

    # bool is a subclass of int; never accept it where a number is expected.
    if isinstance(value, bool) and typ is not bool:
        raise ConfigError(f"{name}: expected {typ.__name__}, got a boolean")
    if typ is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, typ):
        raise ConfigError(f"{name}: expected {typ.__name__}, got {type(value).__name__}")
    return value
