from urllib.parse import urlsplit

import httpx
import tldextract

from app.core.config import settings
from app.competitors.providers.tavily import tavily_search
from app.competitors.providers.brave import brave_search


extractor = tldextract.TLDExtract(
    suffix_list_urls=None,
    include_psl_private_domains=True,
)

DIRECTORIES = {
    "google.com",
    "bing.com",
    "g2.com",
    "capterra.com",
    "getapp.com",
    "trustpilot.com",
    "reddit.com",
    "quora.com",
    "youtube.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "medium.com",
    "crunchbase.com",
    "producthunt.com",
    "alternativeto.net",
    "wikipedia.org",
    "github.com",
}


def registered_domain(url: str) -> str | None:
    try:
        parsed = urlsplit(url)

        if parsed.scheme not in {"http", "https"}:
            return None

        if parsed.username or parsed.password:
            return None

        if parsed.port not in {None, 80, 443}:
            return None

        host = (
            (parsed.hostname or "")
            .encode("idna")
            .decode("ascii")
            .lower()
        )

        result = extractor(host)

        return (
            result.top_domain_under_public_suffix
            or None
        )

    except (ValueError, UnicodeError):
        return None


def available_providers():
    """
    Returns only providers that have an API key.

    Examples:
      Tavily only -> [Tavily]
      Brave only  -> [Brave]
      Both        -> preferred provider, then fallback
      Neither     -> clear configuration error
    """
    cfg = settings()

    available = {}

    if cfg.tavily_api_key.strip():
        available["tavily"] = tavily_search

    if cfg.brave_api_key.strip():
        available["brave"] = brave_search

    if not available:
        raise RuntimeError(
            "No search API key configured. "
            "Set TAVILY_API_KEY or BRAVE_API_KEY."
        )

    preference = (
        cfg.competitor_search_provider
        or "auto"
    ).strip().lower()

    if preference not in {
        "auto", "tavily", "brave"
    }:
        raise RuntimeError(
            "COMPETITOR_SEARCH_PROVIDER must be "
            "auto, tavily, or brave"
        )

    if preference == "brave":
        order = ["brave", "tavily"]
    else:
        order = ["tavily", "brave"]

    return [
        (name, available[name])
        for name in order
        if name in available
    ]


def safe_error(exc: Exception) -> str:
    """
    Return diagnostic information without API keys.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code

        descriptions = {
            400: "Invalid search request",
            401: "Invalid or missing API key",
            403: "Access denied or plan unavailable",
            429: "Rate limit or credit quota reached",
            500: "Search provider server error",
            502: "Search provider gateway error",
            503: "Search provider unavailable",
        }

        reason = descriptions.get(
            code, "HTTP request failed"
        )

        return f"HTTP {code}: {reason}"

    if isinstance(exc, httpx.TimeoutException):
        return "Network timeout"

    if isinstance(exc, httpx.ConnectError):
        return "Could not connect to search provider"

    return type(exc).__name__


async def search_all(
    queries: list[str],
    own_domains: set[str],
    max_candidates: int = 10,
) -> tuple[list[dict], int, list[str]]:

    providers = available_providers()

    grouped: dict[str, dict] = {}

    hit_count = 0
    errors = []

    for query in queries[:5]:
        results = None
        failures = []

        # Try only providers with valid configured keys.
        for provider_name, search_function in providers:
            try:
                response = await search_function(
                    query,
                    max_results=10,
                )

                if response:
                    results = response
                    break

                # Empty results may be valid.
                # Try fallback, if available.
                results = []
                failures.append(
                    f"{provider_name}: no results"
                )

            except Exception as exc:
                failures.append(
                    f"{provider_name}: "
                    f"{safe_error(exc)}"
                )

        if not results:
            errors.append(
                f"Query '{query[:80]}': "
                + "; ".join(failures)
            )
            continue

        for rank, result in enumerate(results):
            hit_count += 1

            url = result.get("url", "")
            domain = registered_domain(url)

            if not domain:
                continue

            if domain in DIRECTORIES:
                continue

            if domain in own_domains:
                continue

            candidate = grouped.setdefault(
                domain,
                {
                    "domain": domain,
                    "website_url": (
                        f"https://{domain}/"
                    ),
                    "name": (
                        domain.split(".")[0].title()
                    ),
                    "hits": [],
                },
            )

            candidate["hits"].append({
                "query": query,
                "url": url,
                "title": result.get("title", ""),
                "rank": rank + 1,
            })

    ranked = sorted(
        grouped.values(),
        key=lambda item: (
            -len({
                hit["query"]
                for hit in item["hits"]
            }),
            min(
                hit["rank"]
                for hit in item["hits"]
            ),
            item["domain"],
        ),
    )

    return (
        ranked[:max_candidates],
        hit_count,
        errors,
    )