from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import httpx

from gtm_crawler.config import FetchSettings, IdentitySettings, UrlSettings
from gtm_crawler.fetch.http import HttpFetcher

IDENTITY = IdentitySettings(user_agent_contact="https://crawler.example/contact")

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_fetcher(
    handler: Handler, sleep: RecordingSleep | None = None, **fetch_overrides: Any
) -> HttpFetcher:
    return HttpFetcher(
        replace(FetchSettings(), **fetch_overrides),
        IDENTITY,
        UrlSettings(),
        transport=httpx.MockTransport(handler),
        sleep=sleep or RecordingSleep(),
    )


def site(routes: dict[str, httpx.Response | Callable[[], httpx.Response]]) -> Handler:
    """Handler serving fixed responses by full URL; anything else is a 404."""

    def handle(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        return route() if callable(route) else route

    return handle


def response(status: int = 200, body: bytes = b"", **headers: str) -> httpx.Response:
    """A streamable response, as a real transport produces.

    ``httpx.Response(content=...)`` reads (and decodes) its body eagerly, which
    both hides the streaming path and fails on deliberately corrupt encodings.
    """
    headers.setdefault("Content-Length", str(len(body)))
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))
