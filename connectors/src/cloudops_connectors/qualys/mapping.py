"""Qualys host detection -> ``cloudops.vulnerability`` row.

The Host List VM Detection API (``/api/2.0/fo/asset/host/vm/detection/``)
returns hosts, each with its detections. Titles, solutions and threat text are
not in it: they come from the KnowledgeBase API, fetched for the QIDs a page
names and stored in bronze beside it.

Naming is FQDN-first (``VMUKWEBP01.NORTHWIND.LOCAL``), so resource matching is
on ``name_norm`` plus IP (LLD, Identity resolution), after Qualys's asset id.

The customer is the host's tag under ``Customer:`` (one Getronics subscription,
customers told apart by tag). A host with no such tag is ``(no customer tag)``,
quarantined like ServiceNow's ``(no company)``; one with two is rejected and
counted, never guessed between.

Every field not mapped, on the host or the detection, is kept in ``raw_extra``.
Results and KB text are data, never instruction (CLAUDE.md rule 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from cloudops_connectors.core.xmlrec import as_list, parse, record

SOURCE: Final = "qualys"
ENTITY: Final = "host_detection"
ENTITIES: Final = frozenset({ENTITY})
CUSTOMER_TAG_PREFIX: Final = "Customer:"
NO_CUSTOMER: Final = "(no customer tag)"

HOST_MAPPED: Final = frozenset({
    "ID", "ASSET_ID", "IP", "DNS", "DNS_DATA", "NETBIOS", "OS", "TAGS", "TRACKING_METHOD",
    "DETECTION_LIST",
})
DETECTION_MAPPED: Final = frozenset({
    "QID", "SEVERITY", "STATUS", "FIRST_FOUND_DATETIME", "LAST_FOUND_DATETIME",
    "LAST_FIXED_DATETIME", "LAST_PROCESSED_DATETIME",
})
# Present on every host Qualys reports: a page without them has drifted.
HOST_EXPECTED: Final = frozenset({"ID", "TRACKING_METHOD", "DETECTION_LIST"})

# KnowledgeBase categories that describe the operating system; the rest are
# applications. Configuration of the mapping, not of a customer.
OS_CATEGORIES: Final = frozenset({
    "Windows", "Ubuntu", "RedHat", "Debian", "SUSE", "CentOS", "Amazon Linux", "OEL",
    "Local", "General remote services", "TCP/IP", "Security Policy", "Information gathering",
})


class MalformedRecordError(ValueError):
    """A host or detection without the fields that key it. Kept in bronze,
    counted, skipped."""


@dataclass(frozen=True)
class KbEntry:
    title: str
    scope: str  # os | application
    solution: str | None
    threat: str | None


@dataclass(frozen=True)
class Detection:
    host_id: str
    asset_id: str | None
    asset_name: str
    ip: str | None
    operating_system: str | None
    customer_raw: str
    tags: tuple[str, ...]
    detection_type: str
    qid: int
    severity: int
    status: str
    first_detected: datetime
    last_detected: datetime | None
    remediated_at: datetime | None
    processed_at: datetime
    raw_extra: dict[str, Any] = field(default_factory=dict)

    @property
    def span_days(self) -> Decimal | None:
        if self.last_detected is None:
            return None
        seconds = Decimal((self.last_detected - self.first_detected).total_seconds())
        return (seconds / Decimal(86400)).quantize(Decimal("0.001"))  # not money: seconds per day


def _time(value: Any, name: str, *, required: bool = False) -> datetime | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        if required:
            raise MalformedRecordError(f"{name} is required")
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError(f"{name} is not an ISO date-time") from None
    if moment.tzinfo is None:
        raise MalformedRecordError(f"{name} has no time zone")
    return moment.astimezone(UTC)


def _required(value: Any, name: str) -> datetime:
    moment = _time(value, name, required=True)
    if moment is None:  # pragma: no cover - _time raises first
        raise MalformedRecordError(f"{name} is required")
    return moment


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def hosts(body: bytes) -> list[dict[str, Any]]:
    root = parse(body)
    host_list = root.find("RESPONSE/HOST_LIST")
    return [] if host_list is None else [record(h) for h in host_list.findall("HOST")]


def next_id_min(body: bytes) -> str | None:
    """Qualys truncates long lists and says where to resume in a WARNING URL."""
    root = parse(body)
    url = root.findtext("RESPONSE/WARNING/URL")
    if not url:
        return None
    for part in url.split("?", 1)[-1].split("&"):
        key, _, value = part.partition("=")
        if key == "id_min" and value.isdigit():
            return value
    return None


def qids(records: list[dict[str, Any]]) -> list[int]:
    out: set[int] = set()
    for host in records:
        dl = host.get("DETECTION_LIST")
        for d in as_list(dl.get("DETECTION") if isinstance(dl, Mapping) else None):
            if isinstance(d, Mapping) and str(d.get("QID", "")).isdigit():
                out.add(int(d["QID"]))
    return sorted(out)


def knowledge_base(body: bytes) -> dict[int, KbEntry]:
    root = parse(body)
    out: dict[int, KbEntry] = {}
    for vuln in root.findall("RESPONSE/VULN_LIST/VULN"):
        v = record(vuln)
        qid, title = str(v.get("QID", "")), _text(v.get("TITLE"))
        if not qid.isdigit() or title is None:
            continue
        category = _text(v.get("CATEGORY")) or ""
        out[int(qid)] = KbEntry(
            title=title,
            scope="os" if category in OS_CATEGORIES else "application",
            solution=_text(v.get("SOLUTION")),
            threat=_text(v.get("DIAGNOSIS")),
        )
    return out


def _customer(tags: list[str]) -> str:
    named = sorted({t[len(CUSTOMER_TAG_PREFIX):].strip() for t in tags
                    if t.startswith(CUSTOMER_TAG_PREFIX)} - {""})
    if len(named) > 1:
        raise MalformedRecordError("host carries more than one customer tag")
    return named[0] if named else NO_CUSTOMER


def normalise(host: Mapping[str, Any]) -> tuple[list[Detection], int]:
    """A host's detections, and how many of them were malformed. A host without
    its id or a usable name rejects every detection it carries."""
    host_id = _text(host.get("ID"))
    dns_data = host.get("DNS_DATA")
    fqdn = _text(dns_data.get("FQDN")) if isinstance(dns_data, Mapping) else None
    name = fqdn or _text(host.get("DNS")) or _text(host.get("NETBIOS")) or _text(host.get("IP"))
    dl = host.get("DETECTION_LIST")
    raw = as_list(dl.get("DETECTION") if isinstance(dl, Mapping) else None)
    if host_id is None or name is None:
        return [], len(raw) or 1
    tags_el = host.get("TAGS")
    tags = [str(t.get("NAME", "")).strip() for t in as_list(
        tags_el.get("TAG") if isinstance(tags_el, Mapping) else None) if isinstance(t, Mapping)]
    try:
        customer = _customer(tags)
    except MalformedRecordError:
        return [], len(raw) or 1
    host_extra = {k: v for k, v in host.items() if k not in HOST_MAPPED}
    tracking = _text(host.get("TRACKING_METHOD")) or ""
    out: list[Detection] = []
    bad = 0
    for d in raw:
        if not isinstance(d, Mapping):
            bad += 1
            continue
        try:
            qid, severity = str(d.get("QID", "")), str(d.get("SEVERITY", ""))
            if not qid.isdigit() or severity not in {"1", "2", "3", "4", "5"}:
                raise MalformedRecordError("QID and SEVERITY 1-5 are required")
            status = _text(d.get("STATUS")) or "Active"
            first = _required(d.get("FIRST_FOUND_DATETIME"), "FIRST_FOUND_DATETIME")
            processed = _required(d.get("LAST_PROCESSED_DATETIME"), "LAST_PROCESSED_DATETIME")
            fixed = _time(d.get("LAST_FIXED_DATETIME"), "LAST_FIXED_DATETIME")
            out.append(Detection(
                host_id=host_id,
                asset_id=_text(host.get("ASSET_ID")),
                asset_name=name,
                ip=_text(host.get("IP")),
                operating_system=_text(host.get("OS")),
                customer_raw=customer,
                tags=tuple(t for t in tags if t),
                detection_type="Cloud Agent" if tracking == "AGENT" else "Remote",
                qid=int(qid),
                severity=int(severity),
                status=status,
                first_detected=first,
                last_detected=_time(d.get("LAST_FOUND_DATETIME"), "LAST_FOUND_DATETIME"),
                remediated_at=fixed if status == "Fixed" else None,
                processed_at=processed,
                raw_extra={
                    **({"host": host_extra} if host_extra else {}),
                    **{k: v for k, v in d.items() if k not in DETECTION_MAPPED},
                },
            ))
        except MalformedRecordError:
            bad += 1
    return out, bad


def processed_times(host: Mapping[str, Any]) -> list[datetime]:
    """Every parseable LAST_PROCESSED_DATETIME, malformed detections included,
    for the watermark."""
    dl = host.get("DETECTION_LIST")
    out = []
    for d in as_list(dl.get("DETECTION") if isinstance(dl, Mapping) else None):
        if isinstance(d, Mapping):
            try:
                t = _time(d.get("LAST_PROCESSED_DATETIME"), "LAST_PROCESSED_DATETIME")
            except MalformedRecordError:
                continue
            if t is not None:
                out.append(t)
    return out
