"""Bronze: the unmodified source payload, stored before anything parses it.

LLD, Connector contract: "persist the raw payload to bronze before parsing
anything". A normalisation defect is usually found weeks later; the raw page
is the only way to fix history without re-querying a source that has since
changed. Replay reads these back (``list``/``get``) and never re-fetches.

Layout: ``{source}/{entity}/{yyyy}/{mm}/{dd}/{run_id}-{page:05d}.json``, dated
by fetch time (UTC). Pages are write-once: storing a page that already exists
succeeds only if the bytes are identical, so a retried fetch activity is
idempotent and a different payload under the same name is an error, never a
silent overwrite.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Final, Protocol

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob.aio import ContainerClient

PAGE_SUFFIX: Final = ".json"


class RawConflictError(Exception):
    """A page name already holds different bytes. Bronze is write-once."""


def page_ref(
    source: str, entity: str, fetched_at: datetime, run_id: str, page: int
) -> str:
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    day = fetched_at.date()
    return f"{source}/{entity}/{day:%Y/%m/%d}/{run_id}-{page:05d}{PAGE_SUFFIX}"


def day_prefixes(source: str, entity: str, start: date, end: date) -> list[str]:
    days = (end - start).days
    if days < 0:
        raise ValueError("end is before start")
    return [
        f"{source}/{entity}/{start + timedelta(days=i):%Y/%m/%d}/" for i in range(days + 1)
    ]


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class RawStore(Protocol):
    async def put(self, ref: str, body: bytes, metadata: Mapping[str, str]) -> None: ...

    async def get(self, ref: str) -> bytes: ...

    async def list(self, prefix: str) -> list[str]:
        """Page references under ``prefix``, sorted."""
        ...


class MemoryRawStore:
    """For tests. Records the order of operations so tests can prove raw is
    written before anything parses it."""

    def __init__(self) -> None:
        self.pages: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.events: list[tuple[str, str]] = []

    async def put(self, ref: str, body: bytes, metadata: Mapping[str, str]) -> None:
        existing = self.pages.get(ref)
        if existing is not None and existing != body:
            raise RawConflictError(ref)
        self.pages[ref] = body
        self.metadata[ref] = dict(metadata)
        self.events.append(("put", ref))

    async def get(self, ref: str) -> bytes:
        self.events.append(("get", ref))
        return self.pages[ref]

    async def list(self, prefix: str) -> list[str]:
        return sorted(r for r in self.pages if r.startswith(prefix))


class BlobRawStore:
    """Bronze on Azure Blob Storage, or Azurite locally."""

    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    async def put(self, ref: str, body: bytes, metadata: Mapping[str, str]) -> None:
        blob = self._container.get_blob_client(ref)
        try:
            await blob.upload_blob(body, overwrite=False, metadata=dict(metadata))
        except ResourceExistsError:
            stored = await (await blob.download_blob()).readall()
            if stored != body:
                raise RawConflictError(ref) from None

    async def get(self, ref: str) -> bytes:
        stream = await self._container.get_blob_client(ref).download_blob()
        return bytes(await stream.readall())

    async def list(self, prefix: str) -> list[str]:
        return sorted([b.name async for b in self._container.list_blobs(name_starts_with=prefix)])
