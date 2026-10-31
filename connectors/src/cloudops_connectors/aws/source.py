"""AWS through the connector contract (``core/ingest.py``): one source, four
entities, dispatched to ``config_source``, ``cur_source`` and ``billing_source``.

- ``config``: one scope, the aggregator. Cursor: Config's ``NextToken``.
- ``cur``: one scope per report (``<prefix>/<report>``), each with its own
  watermark. Cursor: the queue of S3 objects still to read.
- ``cost_total``, ``invoice``: one scope per payer account. Cursor: the
  request's dates and the API's page token.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from cloudops_connectors.aws import config_source, cur_source, mapping
from cloudops_connectors.aws.billing_source import AwsBilling, PayerBilling
from cloudops_connectors.aws.client import ConfigClient, S3Client
from cloudops_connectors.core.ingest import Fetched, PageContext, PageInfo, PageOutcome, Source
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import Database


class AwsSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(
        self, config: ConfigClient, s3: S3Client, reports: Sequence[str],
        *, instance: str | None = None, billing: Sequence[PayerBilling] = (),
        today: Callable[[], date] | None = None,
    ) -> None:
        """``reports`` are CUR report prefixes in the bucket, ``<prefix>/<report>``.
        ``billing`` holds each payer's billing clients; ``today`` is the
        billing entities' run day (UTC by default)."""
        for r in reports:
            if not r or r.startswith("/") or r.endswith("/") or "/" not in r:
                raise ValueError("a CUR report is named <prefix>/<report>")
        self._config = config
        self._s3 = s3
        self._reports = list(reports)
        self._billing = AwsBilling(list(billing), today or (lambda: datetime.now(UTC).date()))
        self.instance = instance or f"{config.base_url}/{config.aggregator}"

    @staticmethod
    def _check(entity: str) -> None:
        if entity not in mapping.ENTITIES:
            raise ValueError(f"unknown entity {entity!r}")

    async def scopes(self, db: Database, entity: str) -> list[str]:
        self._check(entity)
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return self._billing.payers
        return [self._config.aggregator] if entity == mapping.CONFIG else list(self._reports)

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        self._check(entity)
        if entity == mapping.CONFIG:
            return Fetched(await self._config.select(since, cursor), ".json")
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return await self._billing.fetch(entity, scope, cursor)
        return await cur_source.fetch(self._s3, scope, cursor)

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        self._check(entity)
        if entity == mapping.CONFIG:
            return config_source.inspect(body)
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return await self._billing.inspect(raw, entity, ref, body)
        return await cur_source.inspect(raw, scope, ref, body, cursor)

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        self._check(entity)
        if entity == mapping.CONFIG:
            return await config_source.process(db, raw, ref, ctx)
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return await self._billing.process(db, raw, entity, ref, ctx)
        return await cur_source.process(db, raw, ref, ctx)
