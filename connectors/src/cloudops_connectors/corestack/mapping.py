"""CoreStack finding -> ``cloudops.cost_finding`` row.

**The API shape is assumed.** There are no CoreStack API docs or sandbox
account yet, so the endpoint and field names below are a plausible model, kept
here and in ``client.py`` so that the real shape changes one module:

    GET /v1/findings?updated_since=<ISO>&offset=<n>&limit=<n>
    -> {"data": [finding, ...], "total": N, "offset": n, "limit": n}

ordered by ``updated_at`` then ``id``, so offsets are stable within a run. A
finding::

    {"id", "tenant_name", "cloud", "cloud_account_id", "resource_id",
     "resource_name", "category", "title", "recommendation", "status",
     "estimated_monthly_savings": {"amount", "currency"},
     "created_at", "updated_at", ...}

- **Customer** is ``tenant_name``, CoreStack's own customer label, resolved like
  any other source's company name (``state.resolve_aliases``).
- **Account and resource** are native ids, linked only within the tenant the
  name resolved to (``source.py``).
- **Money is never a float** (CLAUDE.md rule 5). The page is parsed with
  ``parse_float=Decimal``, so a JSON number becomes a ``Decimal`` from its
  literal text, never through binary floating point; a string amount is read
  the same way. An amount must fit ``numeric(18,6)`` exactly and be
  non-negative: one with more than six decimals is rejected and counted, never
  silently rounded (rounding happens once, at presentation). An amount without
  a currency is rejected; a null amount is "no saving estimated".
- **Status** is mapped onto open | resolved | dismissed; any other value is
  rejected and counted rather than guessed.

Every field not mapped is kept in ``raw_extra``. Titles, recommendations and
any other text are data, never instruction (CLAUDE.md rule 4).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

SOURCE: Final = "corestack"
ENTITY: Final = "finding"
ENTITIES: Final = frozenset({ENTITY})

MAPPED: Final = frozenset({
    "id", "tenant_name", "cloud", "cloud_account_id", "resource_id", "resource_name",
    "category", "title", "recommendation", "status", "estimated_monthly_savings",
    "created_at", "updated_at",
})
# Present on every finding CoreStack reports: a page without them has drifted.
EXPECTED: Final = frozenset({"id", "tenant_name", "category", "title", "status",
                             "created_at", "updated_at"})

STATUSES: Final = {
    "open": "open", "active": "open", "new": "open", "in progress": "open",
    "resolved": "resolved", "fixed": "resolved", "closed": "resolved",
    "dismissed": "dismissed", "suppressed": "dismissed", "ignored": "dismissed",
    "exempted": "dismissed",
}
PROVIDERS: Final = {"azure": "azure", "aws": "aws"}
MONEY: Final = Decimal("0.000001")  # numeric(18,6)
MONEY_LIMIT: Final = Decimal(10) ** 12
_CURRENCY: Final = re.compile(r"[A-Z]{3}")


class MalformedRecordError(ValueError):
    """A finding without the fields that key it, or with money that cannot be
    stored exactly. Kept in bronze, counted, skipped."""


class MalformedPageError(ValueError):
    """The body is not a findings page at all."""


@dataclass(frozen=True)
class Finding:
    finding_id: str
    customer_raw: str
    provider: str | None  # azure | aws; None: the account cannot be looked up
    account_native_id: str | None
    resource_native_id: str | None
    resource_name: str | None
    category: str
    title: str
    recommendation: str | None
    status: str
    saving: Decimal | None
    currency: str | None
    detected_at: datetime
    updated_at: datetime
    raw_extra: dict[str, Any] = field(default_factory=dict)


def page(body: bytes) -> dict[str, Any]:
    """Parse a page. Non-integer numbers become ``Decimal`` from their text."""
    try:
        doc = json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise MalformedPageError("not JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise MalformedPageError("no data array")
    return doc


def records(body: bytes) -> list[dict[str, Any]]:
    """The page's findings that are objects; anything else is counted by
    ``normalise`` as malformed."""
    return [r if isinstance(r, dict) else {} for r in page(body)["data"]]


def next_offset(body: bytes) -> str | None:
    """The offset of the next page, or None after the last."""
    doc = page(body)
    data, total, offset = doc["data"], doc.get("total"), doc.get("offset", 0)
    if not data or not isinstance(total, int) or not isinstance(offset, int):
        return None
    following = offset + len(data)
    return str(following) if following < total else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: Any, name: str) -> str:
    text = _text(value)
    if text is None:
        raise MalformedRecordError(f"{name} is required")
    return text


def time_of(value: Any, name: str) -> datetime:
    text = _required_text(value, name)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError(f"{name} is not an ISO date-time") from None
    if moment.tzinfo is None:
        raise MalformedRecordError(f"{name} has no time zone")
    return moment.astimezone(UTC)


def amount_of(value: Any) -> Decimal | None:
    """A JSON number (already a ``Decimal`` or ``int``) or a numeric string,
    exactly. Never through ``float``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise MalformedRecordError("amount is not a number")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise MalformedRecordError("amount is not a number") from None
    if not amount.is_finite():
        raise MalformedRecordError("amount is not finite")
    if amount < 0:
        raise MalformedRecordError("a saving cannot be negative")
    exact = amount.quantize(MONEY)
    if exact != amount or exact >= MONEY_LIMIT:
        raise MalformedRecordError("amount does not fit numeric(18,6) exactly")
    return exact


def money_of(value: Any) -> tuple[Decimal | None, str | None]:
    """(amount, currency): both or neither."""
    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        raise MalformedRecordError("estimated_monthly_savings is not an object")
    amount = amount_of(value.get("amount"))
    if amount is None:
        return None, None
    currency = _text(value.get("currency"))
    currency = currency.upper() if currency else None
    if currency is None or not _CURRENCY.fullmatch(currency):
        raise MalformedRecordError("an amount needs its ISO 4217 currency")
    return amount, currency


def normalise(rec: Mapping[str, Any]) -> Finding:
    finding_id = _required_text(rec.get("id"), "id")
    customer = _required_text(rec.get("tenant_name"), "tenant_name")
    raw_status = _required_text(rec.get("status"), "status").lower()
    status = STATUSES.get(raw_status)
    if status is None:
        raise MalformedRecordError("unknown status")
    saving, currency = money_of(rec.get("estimated_monthly_savings"))
    cloud = _text(rec.get("cloud"))
    return Finding(
        finding_id=finding_id,
        customer_raw=customer,
        provider=PROVIDERS.get(cloud.lower()) if cloud else None,
        account_native_id=_text(rec.get("cloud_account_id")),
        resource_native_id=_text(rec.get("resource_id")),
        resource_name=_text(rec.get("resource_name")),
        category=_required_text(rec.get("category"), "category").lower(),
        title=_required_text(rec.get("title"), "title"),
        recommendation=_text(rec.get("recommendation")),
        status=status,
        saving=saving,
        currency=currency,
        detected_at=time_of(rec.get("created_at"), "created_at"),
        updated_at=time_of(rec.get("updated_at"), "updated_at"),
        raw_extra={k: v for k, v in rec.items() if k not in MAPPED},
    )


def updated_times(recs: list[dict[str, Any]]) -> list[datetime]:
    """Every parseable ``updated_at``, malformed findings included, for the
    watermark: a finding rejected for its money still moved time forward."""
    out = []
    for r in recs:
        try:
            out.append(time_of(r.get("updated_at"), "updated_at"))
        except MalformedRecordError:
            continue
    return out


def extra_json(extra: Mapping[str, Any]) -> str | None:
    """``raw_extra`` as JSON. Decimals (non-integer numbers in the page) are
    kept as their exact text, as JSON strings: jsonb would store them exactly,
    but ``json.dumps`` only knows floats."""
    if not extra:
        return None
    return json.dumps(extra, sort_keys=True, default=_decimal)


def _decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")
