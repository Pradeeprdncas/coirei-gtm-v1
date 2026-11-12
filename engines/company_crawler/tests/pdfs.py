"""Deterministic PDF fixtures, written by hand (no PDF library needed to make them).

``make_pdf(pages)``: each page a list of ``Text(x, y, size, text, bold)`` and
``Line(x1, y1, x2, y2)`` items in PDF points (origin bottom left, A4 595 x 842).
Text is WinAnsi-encoded Helvetica, so "€" works and characters outside cp1252 do not.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Text:
    x: float
    y: float
    size: float
    text: str
    bold: bool = False


@dataclass(frozen=True)
class Line:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class Image:
    """A 2x2 grey inline image scaled to w x h: a page with only this has no text layer."""

    x: float
    y: float
    w: float
    h: float


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _content(items: list[Text | Line | Image]) -> bytes:
    out = []
    for item in items:
        if isinstance(item, Text):
            font = b"/F2" if item.bold else b"/F1"
            out.append(b"BT " + font + b" %g Tf %g %g Td (" % (item.size, item.x, item.y)
                       + _escape(item.text) + b") Tj ET")  # fmt: skip
        elif isinstance(item, Image):
            out.append(b"q %g 0 0 %g %g %g cm BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\xff\xff\x00 EI Q"
                       % (item.w, item.h, item.x, item.y))  # fmt: skip
        else:
            out.append(b"%g %g m %g %g l S" % (item.x1, item.y1, item.x2, item.y2))
    return b"\n".join(out)


def make_pdf(
    pages: list[list[Text | Line | Image]],
    info: dict[str, str] | None = None,
    encrypt: bool = False,
    blank_pages: tuple[int, ...] = (),
    outline: list[tuple[str, int]] | None = None,
) -> bytes:
    """A PDF 1.4 file; ``info`` -> the document information dictionary; ``encrypt``
    adds a standard-security /Encrypt entry (the file then needs a password);
    ``blank_pages`` (0-based) get an image-only page stand-in: no text at all;
    ``outline``: bookmarks (title, 0-based page)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in below
    pages_id = add(b"")
    fonts = (add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                 b"/Encoding /WinAnsiEncoding >>"),
             add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                 b"/Encoding /WinAnsiEncoding >>"))  # fmt: skip
    kids = []
    for n, items in enumerate(pages):
        stream = b"" if n in blank_pages else _content(items)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>"
            % (pages_id, content, fonts[0], fonts[1])))  # fmt: skip
    outlines = b""
    if outline:
        root = add(b"")
        items = [add(b"") for _ in outline]
        for k, ((title, page), obj) in enumerate(zip(outline, items, strict=True)):
            links = (b" /Prev %d 0 R" % items[k - 1] if k else b"") + (
                b" /Next %d 0 R" % items[k + 1] if k + 1 < len(items) else b""
            )
            objects[obj - 1] = (
                b"<< /Title (" + _escape(title) + b") /Parent %d 0 R /Dest "
                b"[%d 0 R /Fit]" % (root, kids[page]) + links + b" >>"
            )
        objects[root - 1] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (
            items[0],
            items[-1],
            len(items),
        )
        outlines = b" /Outlines %d 0 R" % root
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R" % pages_id + outlines + b" >>"
    objects[pages_id - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                             + b"] /Count %d >>" % len(kids))  # fmt: skip
    trailer_extra = b""
    if info:
        fields = b" ".join(b"/" + k.encode() + b" (" + _escape(v) + b")" for k, v in info.items())
        trailer_extra += b" /Info %d 0 R" % add(b"<< " + fields + b" >>")
    if encrypt:
        o, u = b"<" + b"11" * 32 + b">", b"<" + b"22" * 32 + b">"
        trailer_extra += b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (
            add(b"<< /Filter /Standard /V 1 /R 2 /O " + o + b" /U " + u + b" /P -4 >>"),
            b"ab" * 16, b"ab" * 16)  # fmt: skip
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog) + trailer_extra
    out += b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def paragraph(x: float, y: float, lines: list[str], size: float = 11, leading: float = 14,
              bold: bool = False) -> list[Text]:  # fmt: skip
    return [Text(x, y - i * leading, size, line, bold) for i, line in enumerate(lines)]


def table(x: float, y: float, rows: list[list[str]], widths: list[float], height: float = 18,
          size: float = 10) -> list[Text | Line]:  # fmt: skip
    """A ruled table (every cell boxed), top-left corner at (x, y)."""
    items: list[Text | Line] = []
    right = x + sum(widths)
    for r in range(len(rows) + 1):
        items.append(Line(x, y - r * height, right, y - r * height))
    edge = x
    for w in [0.0, *widths]:
        edge += w
        items.append(Line(edge, y, edge, y - len(rows) * height))
    for r, row in enumerate(rows):
        left = x
        for w, cell in zip(widths, row, strict=True):
            items.append(Text(left + 4, y - (r + 1) * height + 5, size, cell))
            left += w
    return items
