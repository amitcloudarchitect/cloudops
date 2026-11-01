"""Qualys through the connector contract (``core/ingest.py``).

fetch     one detection page (``id_min`` is the cursor)
inspect   after the page is in bronze: the QIDs it names, whose KnowledgeBase
          entries are fetched and stored in bronze beside it (``.kb.xml``)
process   normalise, resolve the customer tags, link resources (asset id, then
          name_norm plus IP, then name_norm), upsert on (tenant, QID, asset
          name, first detected), quarantine, record drift; then the watermark

**Customer.** Each of a host's candidate tags (``mapping.customer_candidates``)
is resolved; the host is filed under the one tenant they resolve to. None
resolving: each candidate is quarantined with a count, for the customer mapping,
and nothing is written. Two tenants: rejected and counted, kept in bronze.

**Simulated** (``simulated=True``, dev only, ADR 0028): the same code over the
simulated estate of ADR 0025; every row it writes says so in ``raw_extra``.

Pages come in host-id order, not time order, so the watermark moves only on
the run's last page, to the highest processed time the run saw: a run that
stops part-way starts again from the same place, and upserts make the repeat
harmless.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    MissingRawError,
    PageContext,
    PageInfo,
    PageOutcome,
    Source,
    principal,
)
from cloudops_connectors.core.raw import RawStore, sha256
from cloudops_connectors.qualys import mapping
from cloudops_connectors.qualys.client import QualysClient
from cloudops_tenancy import Database, Role, TenantScope

_COLUMNS: Final = (
    "tenant_id", "qid", "asset_name", "asset_ipv4", "resource_id", "scope", "title",
    "severity", "operating_system", "solution", "threat", "detection_type", "asset_tags",
    "first_detected", "last_detected", "remediated_at", "detection_span_days", "status",
    "raw_extra",
)
_KEY: Final = ("tenant_id", "qid", "asset_name", "first_detected")
_UPSERT: Final = text(
    f"INSERT INTO cloudops.vulnerability ({', '.join(_COLUMNS)}) VALUES ("  # noqa: S608
    + ", ".join(f"CAST(:{c} AS jsonb)" if c == "raw_extra" else f":{c}" for c in _COLUMNS)
    + f") ON CONFLICT ({', '.join(_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c not in _KEY)
)
_CANDIDATES: Final = text(
    "SELECT id, tenant_id, qualys_asset_id, name_norm, ip_addresses FROM cloudops.resource"
    " WHERE qualys_asset_id = ANY(CAST(:asset_ids AS text[]))"
    "    OR name_norm = ANY(CAST(:norms AS text[]))"
)


def kb_ref(ref: str) -> str:
    """Where a page's KnowledgeBase payload is stored, beside it."""
    return ref.removesuffix(".xml") + ".kb.xml"


class QualysSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(self, client: QualysClient, *, simulated: bool = False) -> None:
        self._client = client
        self._simulated = simulated
        self.instance = client.base_url

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        if entity != mapping.ENTITY:
            raise ValueError(f"unknown entity {entity!r}")
        return Fetched(await self._client.detections(since, cursor), ".xml")

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        hosts = mapping.hosts(body)
        qids = mapping.qids(hosts)
        kb = await self._client.knowledge_base(qids) if qids else (
            b"<KNOWLEDGE_BASE_VULN_LIST_OUTPUT><RESPONSE><VULN_LIST/></RESPONSE>"
            b"</KNOWLEDGE_BASE_VULN_LIST_OUTPUT>")
        await raw.put(kb_ref(ref), kb, {
            "source": self.source, "entity": "knowledge_base", "page_ref": ref,
            "sha256": sha256(kb), "instance": self.instance,
        })
        records = sum(len(found) + bad for found, bad in map(mapping.normalise, hosts))
        return PageInfo(records, mapping.next_id_min(body))

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        if ref.endswith(".kb.xml"):
            return PageOutcome(state.RunCounts())  # a companion payload, read with its page
        hosts = mapping.hosts(await raw.get(ref))
        counts = state.RunCounts(pages=1)
        detections: list[mapping.Detection] = []
        for host in hosts:
            found, bad = mapping.normalise(host)
            detections.extend(found)
            counts.rejected += bad
        counts.records = len(detections) + counts.rejected
        moments = [t for h in hosts for t in mapping.processed_times(h)]
        high = max(moments) if moments else None
        kb: dict[int, mapping.KbEntry] = {}
        if detections:
            try:
                kb = mapping.knowledge_base(await raw.get(kb_ref(ref)))
            except KeyError:
                raise MissingRawError(f"no KnowledgeBase payload beside {ref}") from None

        scope0 = TenantScope((), Role.INGEST, principal(self.source))
        async with db.transaction(scope0) as conn:
            resolved = await state.resolve_aliases(
                conn, self.source, (c for d in detections for c in d.customers))
        by_tenant: dict[UUID, list[mapping.Detection]] = defaultdict(list)
        unmatched: Counter[str] = Counter()
        for d in detections:
            tenants = {t for c in d.customers if (t := resolved[c].tenant_id) is not None}
            if not tenants:
                unmatched.update(d.customers)
                counts.quarantined += 1
            elif len(tenants) > 1:
                counts.rejected += 1  # tags naming two customers; stays in bronze
            elif d.qid not in kb:
                counts.rejected += 1  # no title to give it; stays in bronze
            else:
                by_tenant[tenants.pop()].append(d)

        watermark = None
        if ctx.advance and ctx.final:
            marks = [m for m in (ctx.run_high_water, high) if m is not None]
            watermark = max(marks) if marks else None
        unlinked: Counter[identity.Unlinked] = Counter()
        async with db.transaction(TenantScope(by_tenant, Role.INGEST,
                                              principal(self.source))) as conn:
            rows: list[dict[str, Any]] = []
            if by_tenant:
                mine = [d for ds in by_tenant.values() for d in ds]
                candidates = identity.candidate_rows(tuple(r) for r in await conn.execute(
                    _CANDIDATES, {
                        "asset_ids": sorted({d.asset_id for d in mine if d.asset_id}),
                        "norms": sorted({identity.name_norm(d.asset_name) for d in mine}),
                    }))
                for tenant, ds in by_tenant.items():
                    for d in ds:
                        resource = identity.link(tenant, d.asset_id, d.asset_name, d.ip,
                                                 candidates)
                        if resource is None:
                            unlinked[identity.Unlinked(
                                tenant, f"qualys:{d.host_id}", d.asset_name, None, d.ip)] += 1
                        rows.append(_row(d, tenant, resource, kb[d.qid], self._simulated))
                await conn.execute(_UPSERT, rows)
            await state.quarantine_aliases(conn, self.source, unmatched, ref)
            await identity.quarantine(conn, self.source, unlinked, ref)
            await drift.observe(conn, self.source, entity, drift.fields_of(hosts),
                                mapped=mapping.HOST_MAPPED, expected=mapping.HOST_EXPECTED,
                                records=len(hosts), raw_ref=ref)
            if watermark is not None:
                await state.advance_watermark(conn, self.source, entity, ctx.scope_key,
                                              watermark)
        counts.written = len(rows)
        counts.ci_unlinked = sum(unlinked.values())
        return PageOutcome(counts, None if high is None else high.isoformat())


def _row(
    d: mapping.Detection, tenant: UUID, resource: UUID | None, kb: mapping.KbEntry,
    simulated: bool = False,
) -> dict[str, Any]:
    extra = {**d.raw_extra, "simulated": True} if simulated else d.raw_extra
    return {
        "tenant_id": tenant, "qid": d.qid, "asset_name": d.asset_name, "asset_ipv4": d.ip,
        "resource_id": resource, "scope": kb.scope, "title": kb.title, "severity": d.severity,
        "operating_system": d.operating_system, "solution": kb.solution, "threat": kb.threat,
        "detection_type": d.detection_type, "asset_tags": list(d.tags),
        "first_detected": d.first_detected, "last_detected": d.last_detected,
        "remediated_at": d.remediated_at, "detection_span_days": d.span_days,
        "status": d.status,
        "raw_extra": json.dumps(extra, sort_keys=True) if extra else None,
    }
