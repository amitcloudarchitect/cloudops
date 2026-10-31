"""SharePoint driveItem -> ``cloudops.document`` row, and text -> chunks.

A drive's delta (``/sites/{site}/drive/root/delta``) returns driveItems: the
root, folders and files that changed, and deleted items as tombstones (the
``deleted`` facet). Mapped 1:1: id, name, webUrl, eTag, size,
lastModifiedDateTime and the file's MIME type; the ``folder``, ``file``,
``deleted`` and ``root`` facets decide what the item is. Every other field is
kept in ``raw_extra``.

Extraction is by MIME type: plain text and Markdown are read; everything else
is recorded as ``unsupported`` (Office formats wait for a converter) and folders
as ``folder``. A file larger than ``MAX_TEXT_BYTES``, or one whose bytes are not
UTF-8 text, is ``unsupported`` too.

Document text is data, never instruction (CLAUDE.md rule 4): chunks hold it
verbatim, split on paragraph boundaries; nothing here reads what it says.

Step 17: a document's kind (design, handover, policy, runbook, report, other)
comes from its name and its folder (``parentReference.path``), never from its
text. Each chunk records its position in the document and the page it starts
on, so an answer can cite them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from cloudops_domain.knowledge import DocKind, classify_document, page_at

SOURCE: Final = "sharepoint"
ENTITY: Final = "drive_item"
ENTITIES: Final = frozenset({ENTITY})
SOURCE_KIND: Final = "sharepoint"
TEXT_MIME: Final = frozenset({"text/plain", "text/markdown"})
MAX_TEXT_BYTES: Final = 2 * 1024 * 1024
MAX_CHUNK_CHARS: Final = 2000

MAPPED: Final = frozenset({
    "id", "name", "webUrl", "eTag", "size", "lastModifiedDateTime", "file", "folder",
    "deleted", "root",
})
EXPECTED: Final = frozenset({"id"})  # tombstones carry little else


class MalformedRecordError(ValueError):
    """An item without the fields that key it. Kept in bronze, counted, skipped."""


@dataclass(frozen=True)
class DriveItem:
    item_id: str
    name: str
    deleted: bool
    root: bool
    extraction: str  # text | unsupported | folder
    mime_type: str | None
    web_url: str | None
    etag: str | None
    size: int | None
    modified_at: datetime | None
    raw_extra: dict[str, Any] = field(default_factory=dict)
    folder_path: str | None = None
    doc_kind: DocKind = "other"

    @property
    def wants_content(self) -> bool:
        return not self.deleted and self.extraction == "text"


def source_ref(site: str, item: str) -> str:
    return f"{SOURCE_KIND}:{site}:{item}"


def site_prefix(site: str) -> str:
    return f"{SOURCE_KIND}:{site}:"


def page(body: bytes) -> dict[str, Any]:
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("value"), list):
        raise ValueError("not a Graph collection page")
    return data


def items(body: bytes) -> list[dict[str, Any]]:
    return [i if isinstance(i, dict) else {} for i in page(body)["value"]]


def next_link(body: bytes) -> str | None:
    link = page(body).get("@odata.nextLink")
    return link if isinstance(link, str) and link else None


def delta_link(body: bytes) -> str | None:
    link = page(body).get("@odata.deltaLink")
    return link if isinstance(link, str) and link else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError("lastModifiedDateTime is not an ISO date-time") from None
    if moment.tzinfo is None:
        raise MalformedRecordError("lastModifiedDateTime has no time zone")
    return moment.astimezone(UTC)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def normalise(item: Mapping[str, Any]) -> DriveItem:
    item_id = _text(item.get("id"))
    if item_id is None:
        raise MalformedRecordError("id is required")
    deleted = isinstance(item.get("deleted"), Mapping)
    name = _text(item.get("name"))
    if name is None and not deleted:
        raise MalformedRecordError("name is required")
    file = item.get("file")
    mime = _text(file.get("mimeType")) if isinstance(file, Mapping) else None
    size = item.get("size")
    size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
    if isinstance(item.get("folder"), Mapping) or isinstance(item.get("root"), Mapping):
        extraction = "folder"
    elif mime in TEXT_MIME and (size is None or size <= MAX_TEXT_BYTES):
        extraction = "text"
    else:
        extraction = "unsupported"
    extra = {k: v for k, v in item.items() if k not in MAPPED}
    parent = item.get("parentReference")
    folder = _text(parent.get("path")) if isinstance(parent, Mapping) else None
    folder = folder.split(":", 1)[-1] or "/" if folder else None
    if isinstance(file, Mapping):
        rest = {k: v for k, v in file.items() if k != "mimeType"}
        if rest:
            extra["file"] = rest
    return DriveItem(
        item_id=item_id, name=name or "", deleted=deleted,
        root=isinstance(item.get("root"), Mapping), extraction=extraction, mime_type=mime,
        web_url=_text(item.get("webUrl")), etag=_text(item.get("eTag")), size=size,
        modified_at=_time(item.get("lastModifiedDateTime")), raw_extra=extra,
        folder_path=folder,
        doc_kind=classify_document(name or "", folder) if extraction != "folder" else "other",
    )


def decode(data: bytes) -> str | None:
    """A file's text, or None if it is not UTF-8 text (it is then unsupported).
    PostgreSQL text cannot hold NUL, and NUL means binary anyway."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


_PARAGRAPH: Final = re.compile(r"\n[ \t]*\n")


def chunk(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Paragraphs packed into chunks of at most ``limit`` characters, in order.
    A paragraph longer than ``limit`` is cut at the last whitespace before it
    (or at ``limit`` when there is none). Deterministic: the same text always
    gives the same chunks. Text is otherwise untouched."""
    paragraphs = [p.strip() for p in _PARAGRAPH.split(text.replace("\r\n", "\n"))]
    pieces: list[str] = []
    for p in (p for p in paragraphs if p):
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit + 1)
            cut = cut if cut > 0 else limit
            pieces.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        if p:
            pieces.append(p)
    out: list[str] = []
    for piece in pieces:
        if out and len(out[-1]) + 2 + len(piece) <= limit:
            out[-1] = f"{out[-1]}\n\n{piece}"
        else:
            out.append(piece)
    return out


@dataclass(frozen=True)
class Piece:
    ordinal: int
    page: int
    body: str


def pieces(text: str, limit: int = MAX_CHUNK_CHARS) -> list[Piece]:
    """``chunk``, with each chunk's position and the page it starts on (pages are
    form-feed separated; plain text without any is one page)."""
    out: list[Piece] = []
    cursor = 0
    for ordinal, body in enumerate(chunk(text, limit)):
        head = body.split("\n", 1)[0][:200]
        found = text.find(head, cursor)
        start = found if found >= 0 else cursor
        out.append(Piece(ordinal, page_at(text, start), body))
        cursor = start + 1
    return out
