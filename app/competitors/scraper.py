import asyncio
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.config import settings
from app.services.scrape.core import (
    AGENT,
    canonical,
    parse_html,
    public_url,
)

from app.competitors.candidates import (
    registered_domain,
)


PRIORITY = (
    "/product",
    "/features",
    "/solutions",
    "/pricing",
    "/plans",
    "/platform",
    "/about",
    "/use-cases",
)

SKIP = (
    "/blog",
    "/privacy",
    "/terms",
    "/login",
    "/signup",
    "/careers",
    "/compare",
    "/vs/",
    "/alternatives",
)


async def safe_fetch(
    client: httpx.AsyncClient,
    url: str,
    root_domain: str,
    robots: RobotFileParser,
    max_bytes: int = 2_000_000,
):
    # Manually follow redirects so every hop is checked.
    for _ in range(4):
        if registered_domain(url) != root_domain:
            raise ValueError("Redirect left candidate domain")

        if not await public_url(url):
            raise ValueError("Non-public URL blocked")

        if not robots.can_fetch(AGENT, url):
            raise ValueError("Blocked by robots.txt")

        async with client.stream("GET", url) as response:
            if response.status_code in (
                301, 302, 303, 307, 308
            ):
                location = response.headers.get("location")

                if not location:
                    raise ValueError("Invalid redirect")

                url = canonical(urljoin(url, location))
                continue

            response.raise_for_status()

            content_type = response.headers.get(
                "content-type", ""
            ).lower()

            if (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
            ):
                raise ValueError("Not an HTML page")

            if int(
                response.headers.get(
                    "content-length", "0"
                )
            ) > max_bytes:
                raise ValueError("Page too large")

            content = bytearray()

            async for part in response.aiter_bytes():
                content.extend(part)

                if len(content) > max_bytes:
                    raise ValueError("Page too large")

            return (
                url,
                bytes(content).decode(
                    response.encoding or "utf-8",
                    errors="replace",
                ),
            )

    raise ValueError("Too many redirects")


async def crawl_competitor(
    website_url: str,
    max_pages: int = 4,
) -> tuple[list, list[str]]:
    domain = registered_domain(website_url)

    if not domain:
        raise ValueError("Invalid competitor domain")

    home = f"https://{domain}/"

    if not await public_url(home):
        raise ValueError("Unsafe website")

    pages = []
    warnings = []

    async with httpx.AsyncClient(
        headers={"User-Agent": AGENT},
        timeout=12,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        robots = RobotFileParser()

        robots_url = f"https://{domain}/robots.txt"

        # Establish the robots policy before crawling.
        try:
            response = await client.get(robots_url)

            if response.status_code in (404, 410):
                robots.parse([])
            elif response.status_code == 200:
                if len(response.content) > 1_000_000:
                    raise ValueError("robots.txt too large")

                robots.parse(response.text.splitlines())
            else:
                raise ValueError(
                    "Robots policy unavailable"
                )
        except httpx.HTTPError as exc:
            raise ValueError(
                "Could not read robots.txt"
            ) from exc

        delay = max(
            float(robots.crawl_delay(AGENT) or 0),
            0.5,
        )

        queue = [home]
        seen = set()

        while queue and len(pages) < max_pages:
            queue = list(dict.fromkeys(queue))

            # Homepage first, then important product pages.
            if home in queue and home not in seen:
                url = home
            else:
                queue.sort(
                    key=lambda u: (
                        next(
                            (
                                i for i, key in enumerate(PRIORITY)
                                if key in urlsplit(u).path.lower()
                            ),
                            999,
                        ),
                        len(urlsplit(u).path),
                    )
                )
                url = queue[0]

            queue.remove(url)

            if url in seen:
                continue

            seen.add(url)

            if registered_domain(url) != domain:
                continue

            if any(
                value in urlsplit(url).path.lower()
                for value in SKIP
            ):
                continue

            if not robots.can_fetch(AGENT, url):
                warnings.append(
                    f"robots_blocked: {url}"
                )
                continue

            if pages:
                await asyncio.sleep(delay)

            try:
                final_url, html = await safe_fetch(
                    client, url, domain, robots
                )

                page = parse_html(html, final_url)

                if page.status != "ok":
                    warnings.append(
                        f"needs_render: {final_url}"
                    )
                    continue

                pages.append(page)

                for link, label in page.links:
                    if (
                        registered_domain(link) == domain
                        and not any(
                            x in urlsplit(link).path.lower()
                            for x in SKIP
                        )
                    ):
                        queue.append(link)

            except (
                ValueError,
                httpx.HTTPError,
                UnicodeError,
            ) as exc:
                warnings.append(
                    f"{url}: {type(exc).__name__}"
                )

    return pages, warnings