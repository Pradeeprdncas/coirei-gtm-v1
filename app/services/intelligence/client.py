
import httpx
from fastapi import HTTPException

from app.core.config import settings


async def crawler_request(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
) -> dict:
    cfg = settings()

    if (
        not cfg.crawler_service_token
        or len(cfg.crawler_service_token) < 32
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CRAWLER_NOT_CONFIGURED",
                "message": "Crawler service token is missing",
            },
        )

    headers = {
        "Authorization": (
            f"Bearer {cfg.crawler_service_token}"
        )
    }

    try:
        async with httpx.AsyncClient(
            base_url=cfg.crawler_base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                path,
                json=payload,
            )

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CRAWLER_UNAVAILABLE",
                "message": "Cannot connect to crawler service",
                "details": {
                    "error_type": type(exc).__name__,
                },
            },
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "CRAWLER_UPSTREAM_ERROR",
                "message": "Crawler request failed",
                "details": {
                    "upstream_status": response.status_code,
                },
            },
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "INVALID_CRAWLER_RESPONSE",
                "message": "Crawler returned invalid JSON",
            },
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "INVALID_CRAWLER_RESPONSE",
                "message": "Expected a JSON object",
            },
        )

    return data
