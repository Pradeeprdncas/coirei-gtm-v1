"""A bounded, respectful, same-site website fetcher. No anti-bot or robots bypass."""
import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import httpx
from bs4 import BeautifulSoup
from app.core.config import settings

AGENT = 'CoireiOnboardingBot/0.1 (+respectful research; contact site owner)'
PREFER = ('/products/', '/product/', '/pricing', '/plans', '/about', '/solutions', '/platform',
          '/features', '/customers', '/use-cases', '/our-story', '/agents', '/contact')
SKIP = ('/blog', '/career', '/privacy', '/terms', '/legal', '/login', '/signin', '/signup',
        '/comparison', '/compare', '/versus', '/vs/', '/cookies', '/cart', '/checkout')


@dataclass
class Page:
    url: str
    title: str
    h1: str
    text: str
    page_type: str
    links: list[tuple[str, str]]
    structured: dict
    status: str = 'ok'


def canonical(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', '', ''))


def within(url: str, host: str) -> bool:
    p = urlsplit(url)
    name = (p.hostname or '').lower()
    allowed_host = host.lower().removeprefix('www.')
    return p.scheme in {'https','http'} and (name == allowed_host or name == 'www.' + allowed_host)


def type_of(url: str) -> str:
    path = urlsplit(url).path.lower()
    for kind, words in [('pricing', ('pricing','plans')), ('product', ('product','platform','solutions','agents')),
                        ('about', ('about','our-story')), ('features', ('feature','use-cases')),
                        ('contact', ('contact',))]:
        if any(word in path for word in words):
            return kind
    return 'home' if path in ('', '/') else 'other'


def parse_html(html: str, url: str) -> Page:
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    h1 = soup.h1.get_text(' ', strip=True) if soup.h1 else ''
    links = []
    for a in soup.select('a[href]'):
        href = canonical(urljoin(url, a.get('href', '')))
        label = a.get_text(' ', strip=True)[:120]
        if href.startswith(('http://','https://')):
            links.append((href, label))
    structured = {'json_ld': [], 'metadata': {}, 'title': title, 'h1': h1}
    for meta in soup.select('meta[name], meta[property]'):
        key = meta.get('property') or meta.get('name')
        if key and key.lower() in {'description','og:description','og:title','og:site_name'}:
            structured['metadata'][key.lower()] = meta.get('content','')[:1200]
    for tag in soup.select('script[type="application/ld+json"]')[:20]:
        try:
            raw = json.loads(tag.string or tag.get_text())
            structured['json_ld'].append(raw)
        except (ValueError, TypeError):
            pass
    # Trafilatura typically extracts main text better than soup.get_text; fallback supports plain HTML.
    try:
        import trafilatura
        text = trafilatura.extract(html, include_tables=True, include_comments=False,
                                   output_format='txt') or ''
    except ImportError:
        text = ''
    if len(text.strip()) < 100:
        for node in soup(['script','style','noscript','nav','footer']):
            node.decompose()
        text = soup.get_text('\n', strip=True)
    text = text[:150_000]
    return Page(url, title, h1, text, type_of(url), list(dict.fromkeys(links)), structured,
                status='needs_render' if len(text.strip()) < 160 else 'ok')


async def public_url(url: str) -> bool:
    p = urlsplit(url)
    try:
        if p.scheme not in {'http', 'https'} or p.username or p.password or p.port not in {None,80,443}:
            return False
    except ValueError:
        return False
    hostname = p.hostname
    if not hostname or hostname in {'localhost', 'localhost.localdomain'} or not '.' in hostname:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, p.port or (443 if p.scheme=='https' else 80),
                                        type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def fetch_limited(client: httpx.AsyncClient, url: str, host: str):
    for _ in range(4):
        if not within(url, host) or not await public_url(url):
            raise ValueError('Blocked non-public or off-domain URL')
        async with client.stream('GET', url) as resp:
            if resp.status_code in (301,302,303,307,308):
                loc = resp.headers.get('location')
                if not loc:
                    raise ValueError('Redirect missing Location header')
                url = canonical(urljoin(url, loc))
                continue
            resp.raise_for_status()
            length = int(resp.headers.get('content-length',0))
            if length > settings().scrape_max_bytes:
                raise ValueError('Page is too large')
            data = bytearray()
            async for block in resp.aiter_bytes():
                data.extend(block)
                if len(data) > settings().scrape_max_bytes:
                    raise ValueError('Page exceeds byte limit')
            return url, resp.headers.get('content-type',''), data.decode(resp.encoding or 'utf-8', errors='replace'), resp.status_code
    raise ValueError('Too many redirects')


async def crawl(start_url: str, progress=None) -> tuple[list[Page], list[dict]]:
    url = canonical(start_url if '://' in start_url else 'https://' + start_url)
    if not await public_url(url):
        raise ValueError('Website must have a public hostname; private IPs are blocked')
    host = urlsplit(url).hostname.lower().removeprefix('www.')
    warnings: list[dict] = []
    pages: list[Page] = []
    async with httpx.AsyncClient(headers={'User-Agent': AGENT},
                                timeout=settings().scrape_timeout_seconds,
                                follow_redirects=False, trust_env=False) as client:
        rp = RobotFileParser()
        robots_url = f'{urlsplit(url).scheme}://{urlsplit(url).netloc}/robots.txt'
        sitemap_urls = []
        try:
            _, _, robots_text, _ = await fetch_limited(client, robots_url, host)
            rp.parse(robots_text.splitlines())
            sitemap_urls.extend(re.findall(r'(?im)^Sitemap:\s*(\S+)', robots_text))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                rp.parse([])  # 404 robots = no robots restrictions
            else:
                raise ValueError(f'Cannot establish robots policy ({exc.response.status_code})') from exc
        except (ValueError, httpx.RequestError) as exc:
            raise ValueError(f'Cannot establish robots policy: {exc}') from exc
        delay = max(float(rp.crawl_delay(AGENT) or 0), 0.5)
        if not rp.can_fetch(AGENT, url):
            raise ValueError('Homepage blocked by robots.txt; please provide a document instead')
        queue = [url]
        for sm in ([f'{urlsplit(url).scheme}://{urlsplit(url).netloc}/sitemap.xml'] + sitemap_urls[:2]):
            if not within(sm, host) or not rp.can_fetch(AGENT, sm):
                continue
            try:
                _, _, xml, _ = await fetch_limited(client, sm, host)
                urls = re.findall(r'<loc>\s*(https?://[^<]+)\s*</loc>', xml[:1_000_000], re.I)
                queue.extend(canonical(u.replace('&amp;', '&')) for u in urls if within(u, host))
            except (ValueError, httpx.HTTPError):
                pass
        seen = set()
        last_request = 0.0
        while queue and len(pages) < settings().scrape_max_pages:
            queue = list(dict.fromkeys(queue))
            if not pages and url in queue:
                next_url = url
            else:
                queue.sort(key=lambda u: (0 if any(x in urlsplit(u).path.lower() for x in PREFER) else 1,
                                          len(urlsplit(u).path)))
                next_url = queue[0]
            queue.remove(next_url)
            if next_url in seen or not within(next_url, host):
                continue
            seen.add(next_url)
            path = urlsplit(next_url).path.lower()
            if any(bad in path for bad in SKIP):
                continue
            if not rp.can_fetch(AGENT, next_url):
                warnings.append({'url': next_url, 'reason': 'robots_blocked'})
                continue
            if progress:
                await progress(next_url, len(pages), settings().scrape_max_pages)
            loop_time = asyncio.get_running_loop().time()
            await asyncio.sleep(max(0, last_request + delay - loop_time))
            last_request = asyncio.get_running_loop().time()
            try:
                resolved, ct, html, _ = await fetch_limited(client, next_url, host)
                if 'text/html' not in ct and 'application/xhtml+xml' not in ct:
                    warnings.append({'url': next_url, 'reason': 'non_html'}); continue
                page = parse_html(html, resolved)
                pages.append(page)
                if page.status != 'ok':
                    warnings.append({'url': resolved, 'reason': 'needs_render'})
                # Nav and footer link text is analyzed separately from cleaned body text.
                queue.extend(link for link, label in page.links if within(link, host)
                             and not any(x in urlsplit(link).path.lower() for x in SKIP))
            except (ValueError, httpx.HTTPError, UnicodeError) as exc:
                warnings.append({'url': next_url, 'reason': str(exc)[:180]})
        return pages, warnings
