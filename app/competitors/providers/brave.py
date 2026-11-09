import httpx

from app.core.config import settings


async def brave_search(
    query: str,
    max_results: int = 10,
) -> list[dict]:
    cfg = settings()

    if not cfg.brave_api_key:
        raise RuntimeError(
            "BRAVE_API_KEY is not configured"
        )

    async with httpx.AsyncClient(
        timeout=25,
        trust_env=False,
    ) as client:
        response = await client.get(
            (
                "https://api.search.brave.com/"
                "res/v1/web/search"
            ),
            headers={
                "X-Subscription-Token": (
                    cfg.brave_api_key
                ),
                "Accept": "application/json",
            },
            params={
                "q": query,
                "count": min(max_results, 10),
                "search_lang": "en",
                "safesearch": "moderate",
            },
        )

        response.raise_for_status()
        data = response.json()

    return [
        {
            "url": item["url"],
            "title": item.get("title", ""),
            "snippet": item.get("description", ""),
        }
        for item in data.get("web", {}).get(
            "results", []
        )
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
    ]   