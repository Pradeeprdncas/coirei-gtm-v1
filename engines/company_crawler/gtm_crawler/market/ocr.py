"""Optional OCR of images on fetched pages (adapted from competitor_analy2's ``ocr.py``).

Market-size figures are often published only inside infographics. With
``market.ocr_enabled`` and Tesseract installed (``pip install -e .[ocr]``
plus the Tesseract binary), up to ``ocr_max_images_per_page`` content images
of a fetched page are downloaded through the same guarded fetcher and
robots.txt check as pages, size-limited, and read. OCR text is error-prone:
evidence from it carries an ``image`` locator and a lower extraction
certainty, and it is never the only basis of a verified figure.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Any

import lxml.html

from ..config import MarketSettings
from ..fetch.http import HttpFetcher
from ..policy import PolicyEngine

log = logging.getLogger(__name__)

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_SKIP_EXTENSIONS = (".svg", ".gif", ".ico")
_SKIP_HINTS = ("sprite", "favicon", "pixel", "tracking", "spacer", "blank", "avatar", "emoji",
               "logo", "icon", "badge", "banner-ad")  # fmt: skip
MIN_WIDTH, MIN_HEIGHT = 200, 60
MAX_PIXELS = 40_000_000  # refuse decompression-bomb images


def image_urls(body: bytes, base_url: str, market: MarketSettings) -> list[str]:
    """Content image URLs of a page, in document order, at most the configured number."""
    try:
        doc = lxml.html.fromstring(body, base_url=base_url)
    except (ValueError, lxml.etree.ParserError):
        return []
    doc.make_links_absolute(base_url, resolve_base_href=False)
    out: list[str] = []
    for img in doc.iter("img"):
        src = (img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "").strip()
        lowered = src.lower().split("?")[0]
        if (not lowered.startswith(("http://", "https://")) or lowered.endswith(_SKIP_EXTENSIONS)
                or any(h in lowered for h in _SKIP_HINTS) or src in out):  # fmt: skip
            continue
        out.append(src)
        if len(out) >= market.ocr_max_images_per_page:
            break
    return out


def clean(text: str) -> str:
    """Whitespace-collapsed OCR text; '' when it is short or mostly garbage."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 15:
        return ""
    readable = sum(1 for c in text if c.isalnum() or c.isspace())
    return text if readable / len(text) >= 0.7 else ""


class Ocr:
    def __init__(self, market: MarketSettings) -> None:
        self.market = market
        self.available = False
        self.reason = "disabled (market.ocr_enabled = false)"
        self._tesseract: Any = None
        self._image: Any = None
        if not market.ocr_enabled:
            return
        try:
            import pytesseract  # type: ignore[import-not-found,unused-ignore]
            from PIL import Image  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            self.reason = "pytesseract / Pillow not installed (pip install -e .[ocr])"
            return
        if market.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = market.tesseract_cmd
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001 - any failure means unavailable
            self.reason = f"Tesseract not available: {exc}"[:200]
            return
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        self._tesseract, self._image = pytesseract, Image
        self.available, self.reason = True, "ok"

    def read_bytes(self, data: bytes) -> str:
        """OCR one image; '' for anything unreadable, small or not text."""
        try:
            img = self._image.open(io.BytesIO(data))
            img.load()
        except Exception:  # noqa: BLE001 - untrusted bytes
            return ""
        if img.width < MIN_WIDTH or img.height < MIN_HEIGHT:
            return ""
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = self._image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        img = img.convert("L")
        if max(img.size) > 3000:
            img.thumbnail((3000, 3000))
        try:
            return clean(self._tesseract.image_to_string(img, timeout=30))
        except Exception as exc:  # noqa: BLE001 - OCR failure loses one image only
            log.info("market.ocr_failed", extra={"error": str(exc)[:200]})
            return ""

    async def read(
        self, fetcher: HttpFetcher, policy: PolicyEngine, urls: list[str]
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for url in urls:
            if not (await policy.check(url, external=True)).allowed:
                continue
            result = await fetcher.fetch(url, IMAGE_TYPES)
            body = result.body
            if not result.ok or body is None or len(body) > self.market.ocr_max_image_bytes:
                continue
            text = await asyncio.to_thread(self.read_bytes, body)
            if text:
                out.append((url, text))
        return out
