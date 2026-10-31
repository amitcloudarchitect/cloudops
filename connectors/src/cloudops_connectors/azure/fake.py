"""A fake Azure management plane serving recorded fixtures through httpx.

    fake = FakeAzure.from_fixtures()
    client = AzureClient(FIXTURE_HOST, transport=fake.transport())

It answers the two APIs as the client uses them: ARM's subscription listing,
``subs_page`` per page with an absolute ``nextLink``; and Resource Graph, rows
of the requested subscriptions only, in id order, ``$top`` per page with an
opaque ``$skipToken``. Resource Graph responses carry the user quota headers;
the quota refills once spent (the client is expected to have waited). Tests
can add or remove resources, force 429s with ``Retry-After``, or failures.

It also answers the billing entities' two APIs with recorded responses
(``billing/query/<sub>.json``, ``billing/invoices/<sub>.json``): the Cost
Management query per subscription (an empty result for one with nothing
recorded) and the billing account's invoices per subscription. A query grouped
by day (``cost_daily``) gets the recorded rows of ``billing/daily/<sub>.json``
inside its timePeriod, ``daily_page`` rows a page, the next by ``nextLink``.

``FakeBlobStore`` is the storage account the FOCUS exports land in, in
*stages* like ``FakeS3``: each stage adds or overwrites blobs (a restating run,
a part that lands late). It answers List Blobs (XML, ``maxresults`` and
markers) and Get Blob with ``Last-Modified``, 404 ``BlobNotFound`` for a blob
not delivered yet, and 503 ``ServerBusy`` on demand. Parts are committed as
plain CSV (reviewable) and served gzip-compressed, as the export writes them.

Both check the bearer token when ``bearer`` is set (the live path's contract,
ADR 0023): a request without exactly that token gets the service's own 401,
``AuthenticationFailed`` from ARM, ``NoAuthenticationInformation`` from Storage.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from importlib import resources
from typing import Any, Final
from urllib.parse import unquote
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from cloudops_connectors.azure.client import GRAPH_PATH, SUBSCRIPTIONS_PATH

FIXTURE_HOST: Final = "https://management.azure.fixture.example"
BLOB_HOST: Final = "https://exports.blob.fixture.example"
BILLING_ACCOUNT: Final = "12345678:getronics-fixture_2019-05-31"
BILLING_DAY: Final = "2026-10-28"  # the day the billing fixtures were recorded
_COST_QUERY: Final = re.compile(
    r"/subscriptions/([0-9a-f-]{36})/providers/Microsoft\.CostManagement/query")
_INVOICES: Final = re.compile(
    r"/providers/Microsoft\.Billing/billingAccounts/([^/]+)/billingSubscriptions/"
    r"([0-9a-f-]{36})/invoices")
_EMPTY_QUERY: Final = (b'{"properties": {"nextLink": null, "columns": [{"name": "totalCost",'
                       b' "type": "Number"}, {"name": "Currency", "type": "String"}],'
                       b' "rows": []}}')


def load_fixture(name: str) -> bytes:
    return resources.files("cloudops_connectors.azure").joinpath(f"fixtures/{name}").read_bytes()


def _fixtures_in(folder: str) -> dict[str, bytes]:
    """``{stem: bytes}`` for every file in a fixture folder (none: empty)."""
    base = resources.files("cloudops_connectors.azure").joinpath(f"fixtures/{folder}")
    if not base.is_dir():
        return {}
    return {f.name.removesuffix(".json"): f.read_bytes() for f in base.iterdir()
            if f.name.endswith(".json")}


def _token(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _offset(token: str | None) -> int:
    if not token:
        return 0
    return int(json.loads(base64.urlsafe_b64decode(token.encode()))["offset"])


def _authorised(request: httpx.Request, bearer: str | None) -> bool:
    return bearer is None or request.headers.get("Authorization") == f"Bearer {bearer}"


def _id(row: Any) -> str:
    return str(row.get("id") or "") if isinstance(row, dict) else ""


class FakeAzure:
    def __init__(self, subscriptions: bytes, resources_: bytes, *, quota: int = 15,
                 subs_page: int = 100, queries: dict[str, bytes] | None = None,
                 invoices: dict[str, bytes] | None = None,
                 daily: dict[str, bytes] | None = None, daily_page: int = 1000) -> None:
        self.queries = dict(queries or {})  # subscription -> recorded query response
        self.daily = dict(daily or {})  # subscription -> recorded daily response
        self.daily_page = daily_page
        self.invoices = dict(invoices or {})  # subscription -> recorded invoice listing
        self.subscriptions: list[dict[str, Any]] = json.loads(subscriptions)["value"]
        self.rows: list[Any] = json.loads(resources_)["data"]
        self.quota = quota
        self.remaining = quota
        self.subs_page = subs_page
        self.throttle_next = 0
        self.fail_next = 0
        self.bearer: str | None = None  # when set, every request must carry it
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls, **kwargs: int) -> FakeAzure:
        return cls(load_fixture("subscriptions.json"), load_fixture("resources.json"),
                   queries=_fixtures_in("billing/query"),
                   invoices=_fixtures_in("billing/invoices"),
                   daily=_fixtures_in("billing/daily"), **kwargs)

    def remove(self, native_id: str) -> dict[str, Any]:
        """The resource is gone from Azure."""
        for i, row in enumerate(self.rows):
            if _id(row) == native_id:
                removed: dict[str, Any] = self.rows.pop(i)
                return removed
        raise KeyError(native_id)

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(copy.deepcopy(row))

    def edit(self, native_id: str, **fields: Any) -> None:
        for row in self.rows:
            if _id(row) == native_id:
                row.update(fields)
                return
        raise KeyError(native_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _quota(self) -> dict[str, str]:
        return {
            "x-ms-user-quota-remaining": str(max(self.remaining, 0)),
            "x-ms-user-quota-resets-after": "00:00:05",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not _authorised(request, self.bearer):
            return httpx.Response(401, json={"error": {"code": "AuthenticationFailed"}},
                                  headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(429, headers={**self._quota(), "Retry-After": "7"},
                                  json={"error": {"code": "RateLimiting"}})
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, content=b"unavailable")
        path = request.url.path
        if path == SUBSCRIPTIONS_PATH and request.method == "GET":
            return httpx.Response(200, json=self._subscriptions(request.url.params))
        if path == GRAPH_PATH and request.method == "POST":
            if self.remaining <= 0:
                self.remaining = self.quota  # the window has passed
            self.remaining -= 1
            return httpx.Response(200, headers=self._quota(),
                                  content=self._graph(json.loads(request.content)))
        query = _COST_QUERY.fullmatch(path)
        if query and request.method == "POST":
            body = json.loads(request.content)
            if (body.get("dataset") or {}).get("grouping"):
                return self._daily(request, query.group(1), body)
            if (body.get("type"), body.get("timeframe")) != ("ActualCost", "MonthToDate"):
                return httpx.Response(400, json={"error": {"code": "BadRequest"}})
            return httpx.Response(200, content=self.queries.get(query.group(1), _EMPTY_QUERY),
                                  headers={"Content-Type": "application/json"})
        invoices = _INVOICES.fullmatch(path)
        if invoices and request.method == "GET":
            if unquote(invoices.group(1)) != BILLING_ACCOUNT:
                return httpx.Response(404, json={"error": {"code": "BillingAccountNotFound"}})
            if not (request.url.params.get("periodStartDate")
                    and request.url.params.get("periodEndDate")):
                return httpx.Response(400, json={"error": {"code": "BadRequest"}})
            return httpx.Response(200, content=self.invoices.get(invoices.group(2),
                                                                 b'{"value": []}'),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404, json={"error": {"code": "NotFound"}})

    def _daily(self, request: httpx.Request, sub: str, body: dict[str, Any]) -> httpx.Response:
        grouping = [g.get("name") for g in body["dataset"]["grouping"]]
        period = body.get("timePeriod") or {}
        if (body.get("type"), body.get("timeframe"), body["dataset"].get("granularity"),
                grouping) != ("ActualCost", "Custom", "Daily", ["ServiceName", "ChargeType"]):
            return httpx.Response(400, json={"error": {"code": "BadRequest"}})
        start, end = (int(str(period.get(k, ""))[:10].replace("-", "") or 0)
                      for k in ("from", "to"))
        recorded = json.loads(self.daily.get(sub, b'{"properties": {"rows": [], "columns": '
                                                  b'[{"name": "Cost"}, {"name": "UsageDate"},'
                                                  b' {"name": "ServiceName"}, {"name": '
                                                  b'"ChargeType"}, {"name": "Currency"}]}}'),
                              parse_float=str)
        props = recorded["properties"]
        # A malformed date is the connector's to reject: it passes the period filter.
        rows = [r for r in props["rows"]
                if not 19000101 <= int(r[1]) <= 29991231 or start <= int(r[1]) <= end]
        offset = _offset(request.url.params.get("$skiptoken"))
        page = rows[offset:offset + self.daily_page]
        more = offset + self.daily_page < len(rows)
        link = (str(request.url.copy_with(params={"api-version": "2023-11-01",
                                                  "$skiptoken": _token(offset +
                                                                       self.daily_page)}))
                if more else None)
        text = json.dumps({**recorded, "properties": {**props, "rows": page,
                                                      "nextLink": link}})
        # Amounts were read as strings to keep every digit; send them as numbers again.
        text = re.sub(r'"(-?\d+\.\d+)"', r"\1", text)
        return httpx.Response(200, content=text.encode(),
                              headers={"Content-Type": "application/json"})

    def _subscriptions(self, params: httpx.QueryParams) -> dict[str, Any]:
        start = _offset(params.get("$skiptoken"))
        page = self.subscriptions[start:start + self.subs_page]
        out: dict[str, Any] = {"value": page,
                               "count": {"type": "Total", "value": len(self.subscriptions)}}
        if start + self.subs_page < len(self.subscriptions):
            out["nextLink"] = (f"{FIXTURE_HOST}{SUBSCRIPTIONS_PATH}?api-version=2022-12-01"
                               f"&$skiptoken={_token(start + self.subs_page)}")
        return out

    def _graph(self, body: dict[str, Any]) -> bytes:
        wanted = {str(s).lower() for s in body.get("subscriptions", [])}
        options = body.get("options") or {}
        top = int(options.get("$top", 1000))
        start = _offset(options.get("$skipToken"))
        matching = sorted(
            (r for r in self.rows if isinstance(r, dict)
             and str(r.get("subscriptionId") or "").lower() in wanted),
            key=_id,
        )
        page = matching[start:start + top]
        out: dict[str, Any] = {"totalRecords": len(matching), "count": len(page),
                               "data": page, "facets": [], "resultTruncated": "false"}
        if start + top < len(matching):
            out["$skipToken"] = _token(start + top)
        return json.dumps(out).encode()


# --- the export storage account ---------------------------------------------------------


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class _Stored:
    file: str
    last_modified: datetime


class FakeBlobStore:
    def __init__(self, stages: list[dict[str, _Stored]], files: dict[str, bytes],
                 roots: tuple[str, ...] = ()) -> None:
        self.roots = roots  # the export roots the fixtures hold: <container>/<path>
        self.stages = stages
        self.files = files
        self.blobs: dict[str, _Stored] = {}  # "<container>/<name>"
        self.stage = -1
        self.busy_next = 0
        self.bearer: str | None = None  # when set, every request must carry it
        self.requests: list[httpx.Request] = []
        self.advance()

    @classmethod
    def from_fixtures(cls) -> FakeBlobStore:
        layout = json.loads(load_fixture("exports/stages.json"))
        stages = [{k: _Stored(v["file"], _time(v["last_modified"])) for k, v in s.items()}
                  for s in layout["stages"]]
        names = {o.file for s in stages for o in s.values()}
        return cls(stages, {n: load_fixture(f"exports/{n}") for n in names},
                   tuple(layout["roots"]))

    def advance(self) -> None:
        """The account as the exports leave it at the next stage."""
        self.stage += 1
        self.blobs.update(self.stages[self.stage])

    def body(self, path: str) -> bytes:
        """What Get Blob returns: CSV parts are served gzip-compressed."""
        name = self.blobs[path].file
        data = self.files[name]
        return gzip.compress(data, mtime=0) if name.endswith(".csv") else data

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not _authorised(request, self.bearer):
            return httpx.Response(401, headers={"Content-Type": "application/xml",
                                                "x-ms-error-code": "NoAuthenticationInformation"},
                                  content=b"<?xml version=\"1.0\" encoding=\"utf-8\"?><Error>"
                                          b"<Code>NoAuthenticationInformation</Code></Error>")
        if self.busy_next > 0:
            self.busy_next -= 1
            return httpx.Response(503, content=b"<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                                               b"<Error><Code>ServerBusy</Code></Error>")
        path = unquote(request.url.path).removeprefix("/")
        params = request.url.params
        if "/" not in path and params.get("comp") == "list":
            return self._list(path, params)
        if path not in self.blobs:
            root = Element("Error")
            SubElement(root, "Code").text = "BlobNotFound"
            SubElement(root, "Message").text = "The specified blob does not exist."
            return httpx.Response(404, headers={"Content-Type": "application/xml",
                                                "x-ms-error-code": "BlobNotFound"},
                                  content=tostring(root, encoding="utf-8", xml_declaration=True))
        modified = format_datetime(self.blobs[path].last_modified, usegmt=True)
        return httpx.Response(200, headers={"Last-Modified": modified,
                                            "Content-Type": "application/octet-stream"},
                              content=self.body(path))

    def _list(self, container: str, params: httpx.QueryParams) -> httpx.Response:
        prefix = params.get("prefix", "")
        limit = int(params.get("maxresults", "5000"))
        after = params.get("marker")
        start_after = base64.b64decode(after).decode() if after else ""
        mine = f"{container}/"
        names = sorted(k.removeprefix(mine) for k in self.blobs if k.startswith(mine))
        names = [n for n in names if n.startswith(prefix) and n > start_after]
        page, rest = names[:limit], names[limit:]
        root = Element("EnumerationResults", ServiceEndpoint=f"{BLOB_HOST}/",
                       ContainerName=container)
        SubElement(root, "Prefix").text = prefix
        SubElement(root, "MaxResults").text = str(limit)
        blobs = SubElement(root, "Blobs")
        for n in page:
            b = SubElement(blobs, "Blob")
            SubElement(b, "Name").text = n
            props = SubElement(b, "Properties")
            SubElement(props, "Last-Modified").text = format_datetime(
                self.blobs[mine + n].last_modified, usegmt=True)
            SubElement(props, "Content-Length").text = str(len(self.body(mine + n)))
            SubElement(props, "BlobType").text = "BlockBlob"
        SubElement(root, "NextMarker").text = (
            base64.b64encode(page[-1].encode()).decode() if rest else None)
        return httpx.Response(200, headers={"Content-Type": "application/xml"},
                              content=tostring(root, encoding="utf-8", xml_declaration=True))
