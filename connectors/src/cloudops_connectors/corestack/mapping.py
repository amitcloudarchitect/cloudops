"""CoreStack recommendations -> ``cost_finding`` and ``security_finding`` rows (ADR 0028).

CoreStack's API, as recorded from a real account (``client.py`` has the paths):

    list    {"page_count", "total_count", "recommendations": [rec, ...]}
    rec     {"id", "tenant_id", "tenant_name", "classification", "sub_classification",
             "impact", "status", "name", "policy_id", "policy_name", "source",
             "service_name", "cloud_account_id", "cloud_account_name",
             "resource_counts", "created_at"}
    detail  the list's fields (less id, tenant id, source), plus "description",
            "policy_description", "actions", and "resources" (paged, as the list)
    resource {"id", "resource_id", "resource_name", "resource", "status",
              "location", "resourcegroup"}

There is no change feed (no update time, no "since" filter) and no money. So a
run reads a CoreStack tenant whole (``snapshot``): every list page, and every
recommendation's detail with all its resource pages, into one bronze page (an
envelope of the exact response bodies). An open finding missing from the latest
snapshot is resolved (``source.py``).

- **One finding per recommendation per resource** (``<rec id>:<resource entry
  id>``), so each links to its resource; a recommendation with no resources is
  one finding (``<rec id>``).
- **Where it goes**, by ``classification``: Cost to ``cost_finding`` (savings
  stay NULL: CoreStack states none); Security and Compliance to
  ``security_finding``; anything else (Operation) is kept in bronze and written
  nowhere.
- **Customer** is ``tenant_name``, CoreStack's own customer label, resolved like
  any other source's company name.
- **Account** is the subscription in the resource's id (``cloud_account_id`` is
  CoreStack's internal id, kept in ``raw_extra``). CoreStack lower-cases resource
  ids; linking compares them case-insensitively within the tenant (``source.py``).
- **Severity** is ``impact`` (high, medium, low); **status** open, resolved or
  dismissed: the recommendation's, or for an open one each resource's own. Any
  other value rejects the finding, counted, rather than guess.
- **Times** have no zone; CoreStack's are UTC.
- **A detail CoreStack refuses** (400 or 404 for that one recommendation: its API
  answers 400 for every Defender for Cloud recommendation) does not fail the run.
  The refusal is kept in the envelope with its status, and the recommendation is
  filed from its list fields alone, as one finding with ``detail_status`` in
  ``raw_extra``. Its id is held (``Snapshot.unavailable``), so findings an earlier
  snapshot read from its detail are not resolved because this one could not.

Every field not mapped is kept in ``raw_extra``. Names, descriptions and any other
text are data, never instruction (CLAUDE.md rule 4).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from cloudops_connectors.corestack.client import CoreStackClient, detail_path, list_path

SOURCE: Final = "corestack"
ENTITY: Final = "finding"
ENTITIES: Final = frozenset({ENTITY})
MAX_REQUESTS: Final = 400  # per snapshot; at one a second, inside the 10-minute fetch
# A detail answered with one of these concerns that recommendation alone.
DETAIL_REFUSED: Final = frozenset({400, 404})

MAPPED: Final = frozenset({
    "id", "tenant_id", "tenant_name", "classification", "sub_classification", "impact",
    "status", "name", "policy_name", "source", "created_at",
})
# Present on every recommendation CoreStack lists: a page without them has drifted.
EXPECTED: Final = frozenset({"id", "tenant_name", "classification", "impact", "status", "name",
                             "created_at"})
TABLES: Final = {"cost": "cost", "security": "security", "compliance": "security"}
SEVERITIES: Final = frozenset({"high", "medium", "low"})
STATUSES: Final = {
    "open": "open", "active": "open", "new": "open",
    "resolved": "resolved", "fixed": "resolved", "closed": "resolved",
    "dismissed": "dismissed", "suppressed": "dismissed", "exempted": "dismissed",
    "ignored": "dismissed",
}
_SUBSCRIPTION: Final = re.compile(r"^/subscriptions/([0-9a-fA-F-]{36})(?:/|$)")


class MalformedRecordError(ValueError):
    """A recommendation or resource without the fields that key it, or with a value
    CoreStack does not use. Kept in bronze, counted, skipped."""


class MalformedSnapshotError(ValueError):
    """The page is not a CoreStack snapshot."""


class SnapshotTooLargeError(ValueError):
    """More requests than one snapshot makes. Not retried: half a snapshot would
    resolve the other half."""


# --- the snapshot -----------------------------------------------------------------------


def _page(body: bytes, key: str) -> tuple[int, list[dict[str, Any]]]:
    data = json.loads(body)
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise MalformedSnapshotError(f"a page without {key}")
    count = data.get("page_count")
    return (count if isinstance(count, int) and count > 0 else 1,
            [i if isinstance(i, dict) else {} for i in items])


async def snapshot(client: CoreStackClient, tenant: str, fetched_at: datetime) -> bytes:
    """Every list page, then every recommendation's detail pages, as the envelope
    bronze stores. A page that adds nothing new ends that paging (a server that
    ignores ``page`` would otherwise be read forever)."""
    responses: list[dict[str, Any]] = []
    requests = 0

    async def read(kind: str, rec: str | None, page: int) -> bytes:
        nonlocal requests
        requests += 1
        if requests > MAX_REQUESTS:
            raise SnapshotTooLargeError(f"more than {MAX_REQUESTS} requests")
        if rec is None:
            body = await client.recommendations(tenant, page)
            path = list_path(tenant)
        else:
            body = await client.recommendation(tenant, rec, page)
            path = detail_path(tenant, rec)
        responses.append({"kind": kind, "path": path, "page": page,
                          "body": body.decode("utf-8")})
        return body

    async def pages(kind: str, rec: str | None, key: str, ident: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while True:
            count, items = _page(await read(kind, rec, page), key)
            new = [i for i in items if str(i.get(ident)) not in seen]
            seen.update(str(i.get(ident)) for i in new)
            out.extend(new)
            if page >= count or not new:
                return out
            page += 1

    recs = await pages("list", None, "recommendations", "id")
    for rec in recs:
        if isinstance(rec.get("id"), str) and rec["id"]:
            try:
                await pages("detail", rec["id"], "resources", "id")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in DETAIL_REFUSED:
                    raise
                responses.append({"kind": "detail", "path": detail_path(tenant, rec["id"]),
                                  "page": 1, "status": exc.response.status_code,
                                  "body": exc.response.text[:500]})
    envelope = {"tenant": tenant, "fetched_at": fetched_at.astimezone(UTC).isoformat(),
                "responses": responses}
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Snapshot:
    tenant: str
    fetched_at: datetime
    recommendations: list[dict[str, Any]]  # list fields, detail fields, all resources
    unavailable: frozenset[str] = frozenset()  # ids whose detail CoreStack refused


def parse(body: bytes) -> Snapshot:
    try:
        data = json.loads(body)
        tenant, fetched = str(data["tenant"]), datetime.fromisoformat(data["fetched_at"])
        recs: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        unkeyed: list[dict[str, Any]] = []
        details: dict[str, dict[str, Any]] = {}
        resources: dict[str, list[dict[str, Any]]] = {}
        refused: dict[str, int] = {}
        for r in data["responses"]:
            raw = str(r["body"]).encode("utf-8")
            if r.get("status", 200) != 200:
                if r["kind"] != "detail" or r["status"] not in DETAIL_REFUSED:
                    raise MalformedSnapshotError("a refused page that is not a detail")
                refused[str(r["path"]).rsplit("/", 1)[1]] = int(r["status"])
                continue
            if r["kind"] == "list":
                for rec in _page(raw, "recommendations")[1]:
                    rid = rec.get("id")
                    if not isinstance(rid, str) or not rid:
                        unkeyed.append(rec)
                    elif rid not in recs:
                        recs[rid] = rec
                        order.append(rid)
            else:
                rid = str(r["path"]).rsplit("/", 1)[1]
                page = json.loads(raw)
                details.setdefault(rid, {k: v for k, v in page.items() if k not in (
                    "resources", "page_count", "total_count")})
                resources.setdefault(rid, []).extend(_page(raw, "resources")[1])
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise MalformedSnapshotError("not a CoreStack snapshot") from None
    if fetched.tzinfo is None:
        raise MalformedSnapshotError("fetched_at has no time zone")
    merged = [{**details.get(rid, {}), **recs[rid], "resources": resources.get(rid, []),
               **({"detail_status": refused[rid]} if rid in refused else {})}
              for rid in order]
    return Snapshot(tenant, fetched, merged + unkeyed,
                    frozenset(rid for rid in refused if rid in recs))


# --- findings ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    finding_id: str
    table: str  # cost | security
    category: str  # cost: the sub-classification; security: security | compliance
    subcategory: str | None
    severity: str
    title: str
    policy_name: str | None
    detector: str | None
    recommendation: str | None
    status: str
    customer_raw: str
    provider: str | None
    account_native_id: str | None
    resource_native_id: str | None
    resource_name: str | None
    detected_at: datetime
    raw_extra: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _time(value: Any) -> datetime:
    text = _text(value)
    if text is None:
        raise MalformedRecordError("created_at is required")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError("created_at is not a date-time") from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _status(value: Any) -> str:
    status = STATUSES.get((_text(value) or "").lower())
    if status is None:
        raise MalformedRecordError("status is not one CoreStack uses")
    return status


def account_of(resource_id: str | None) -> tuple[str | None, str | None]:
    """(provider, account id) from a resource's id: an Azure subscription, or the
    account field of an AWS ARN."""
    if not resource_id:
        return None, None
    if (m := _SUBSCRIPTION.match(resource_id)) is not None:
        return "azure", m.group(1).lower()
    parts = resource_id.split(":")
    if resource_id.startswith("arn:") and len(parts) > 4 and parts[4].isdigit():
        return "aws", parts[4]
    return None, None


def findings(rec: Mapping[str, Any]) -> list[Finding]:
    """A recommendation's findings; ``[]`` if it is not one the platform keeps
    (Operation). Raises ``MalformedRecordError`` for a recommendation it cannot key
    or whose values CoreStack does not use."""
    rec_id, customer = _text(rec.get("id")), _text(rec.get("tenant_name"))
    title, classification = _text(rec.get("name")), (_text(rec.get("classification")) or "")
    if rec_id is None or customer is None or title is None:
        raise MalformedRecordError("id, tenant_name and name are required")
    table = TABLES.get(classification.lower())
    severity = (_text(rec.get("impact")) or "").lower()
    if severity not in SEVERITIES:
        raise MalformedRecordError("impact is not one CoreStack uses")
    status = _status(rec.get("status"))
    detected = _time(rec.get("created_at"))
    if table is None:
        return []
    sub = _text(rec.get("sub_classification"))
    extra = {k: v for k, v in rec.items() if k not in MAPPED and k != "resources"}
    if table == "cost":
        extra["impact"] = severity  # cost_finding has no severity column
    resources = rec.get("resources")
    out: list[Finding] = []
    for res in resources if isinstance(resources, list) and resources else [None]:
        if res is not None and not isinstance(res, Mapping):
            raise MalformedRecordError("a resource is not an object")
        entry = _text(res.get("id")) if res is not None else None
        if res is not None and entry is None:
            raise MalformedRecordError("a resource has no id")
        native = _text(res.get("resource_id")) if res is not None else None
        provider, account = account_of(native)
        res_extra = ({f"resource_{k}": v for k, v in res.items() if k != "resource_id"}
                     if res is not None else {})
        out.append(Finding(
            finding_id=f"{rec_id}:{entry}" if entry else rec_id,
            table=table,
            category=(sub or "other").lower() if table == "cost" else classification.lower(),
            subcategory=sub,
            severity=severity,
            title=title,
            policy_name=_text(rec.get("policy_name")),
            detector=_text(rec.get("source")),
            recommendation=_text(rec.get("description")),
            # A recommendation dismissed or resolved is so for every resource; an
            # open one is as open as each resource says.
            status=_status(res.get("status")) if status == "open" and res is not None
            and res.get("status") else status,
            customer_raw=customer,
            provider=provider,
            account_native_id=account,
            resource_native_id=native,
            resource_name=_text(res.get("resource_name")) if res is not None else None,
            detected_at=detected,
            raw_extra={**extra, **res_extra},
        ))
    return out


def extra_json(extra: Mapping[str, Any]) -> str | None:
    return json.dumps(extra, sort_keys=True, default=str) if extra else None
