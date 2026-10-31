"""A small, dependency-free PDF writer for report packs.

It lays out ``render.Line``s on A4 pages with the PDF standard fonts
(Helvetica and Helvetica-Bold, WinAnsi encoding, so the euro and pound signs
print), wraps long text, and draws table rows as aligned cells. That is all a
pack needs, and it keeps a PDF library out of the API image.

The output is deterministic: no creation date, no random ids. The same lines
always give the same bytes, which makes a regenerated pack comparable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from cloudops_domain.reporting.render import Line

PAGE_W: Final = 595
PAGE_H: Final = 842
MARGIN: Final = 50
# Average glyph width as a share of the font size, slightly generous so text
# never runs past the margin.
_WIDTH: Final = {"F1": 0.52, "F2": 0.56}
_STYLE: Final = {
    "title": ("F2", 16, 22), "subtitle": ("F1", 9, 16), "banner": ("F2", 10, 18),
    "heading": ("F2", 13, 22), "subheading": ("F2", 10, 16), "text": ("F1", 10, 14),
    "row": ("F1", 9, 13),
}


def _encode(value: str) -> bytes:
    raw = value.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _wrap(value: str, font: str, size: int, width: float) -> list[str]:
    per_line = max(10, int(width / (size * _WIDTH[font])))
    out: list[str] = []
    for paragraph in value.split("\n"):
        words, current = paragraph.split(), ""
        for word in words:
            while len(word) > per_line:
                if current:
                    out.append(current)
                    current = ""
                out.append(word[:per_line])
                word = word[per_line:]
            candidate = f"{current} {word}".strip()
            if len(candidate) > per_line:
                out.append(current)
                current = word
            else:
                current = candidate
        out.append(current)
    return out


def _text(x: float, y: float, font: str, size: int, value: str) -> bytes:
    return (f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td (".encode()
            + _encode(value) + b") Tj ET\n")


def _pages(lines: Sequence[Line]) -> list[bytes]:
    pages: list[bytes] = []
    body = b""
    y = PAGE_H - MARGIN
    usable = PAGE_W - 2 * MARGIN

    def advance(step: int) -> None:
        nonlocal body, y
        if y - step < MARGIN:
            pages.append(body)
            body, y = b"", PAGE_H - MARGIN
        y -= step

    for line in lines:
        font, size, lead = _STYLE[line.kind]
        if line.kind == "row" and line.cells:
            advance(lead)
            col = usable / len(line.cells)
            for i, cell in enumerate(line.cells):
                limit = max(4, int(col / (size * _WIDTH[font])) - 1)
                shown = cell if len(cell) <= limit else cell[: limit - 1] + "~"
                body += _text(MARGIN + i * col, y, font, size, shown)
            continue
        if line.kind in ("heading", "subheading") and y < PAGE_H - MARGIN:
            advance(lead // 2)
        for piece in _wrap(line.text, font, size, usable):
            advance(lead)
            body += _text(MARGIN, y, font, size, piece)
    pages.append(body)
    return pages


def render_pdf(lines: Sequence[Line], title: str) -> bytes:
    streams = _pages(lines)
    total = len(streams)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # pages, filled in below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold"
        b" /Encoding /WinAnsiEncoding >>",
        b"<< /Title (" + _encode(title) + b") /Producer (CloudOps platform) >>",
    ]
    kids: list[str] = []
    for number, stream in enumerate(streams, start=1):
        footer = _text(MARGIN, MARGIN / 2, "F1", 8, f"Page {number} of {total}")
        content = stream + footer
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                       + content + b"endstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}]"
            f" /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >>"
            f" /Contents {content_id} 0 R >>".encode())
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {total} >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 5 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)
