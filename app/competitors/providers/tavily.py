import httpx

from app.core.config import settings


async def tavily_search(
    query: str,
    max_results: int = 10,
) -> list[dict]:
    cfg = settings()

    if not cfg.tavily_api_key:
        raise RuntimeError(
            "TAVILY_API_KEY is not configured"
        )

    payload = {
        "query": query,
        "search_depth": "basic",
        "max_results": min(max_results, 10),
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }

    async with httpx.AsyncClient(
        timeout=25,
        trust_env=False,
    ) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": (
                    f"Bearer {cfg.tavily_api_key}"
                ),
                "Content-Type": "application/json",
            },
            json=payload,
        )

        response.raise_for_status()
        data = response.json()

    return [
        {
            "url": item["url"],
            "title": item.get("title", ""),
            "snippet": item.get("content", ""),
        }
        for item in data.get("results", [])
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
    ]