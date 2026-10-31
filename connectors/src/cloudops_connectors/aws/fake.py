"""Fake AWS endpoints serving recorded fixtures through httpx.

    config = FakeConfig.from_fixtures()
    client = ConfigClient(REGION, AGGREGATOR, transport=config.transport())
    s3 = FakeS3.from_fixtures()          # the bucket as the first run finds it
    s3.advance()                         # ...and as AWS later restates it

``FakeConfig`` answers ``SelectAggregateResourceConfig`` as the client uses it:
the capture-time filter read from the expression, ``Limit`` and ``NextToken``,
results in no particular order, and 400 ``ThrottlingException`` on demand.

``FakeS3`` is the CUR bucket in *stages*: each stage adds or overwrites objects
(a later assembly overwrites the period's manifest; a late data file appears).
It answers ListObjectsV2 (namespaced XML, ``max-keys`` and continuation tokens)
and GetObject with ``Last-Modified``, 404 ``NoSuchKey`` for an object not
delivered yet, and 503 ``SlowDown`` on demand. Data files are committed as
plain CSV (reviewable) and served gzip-compressed, as AWS delivers them.

``FakeCostExplorer`` and ``FakeInvoicing`` answer one payer's billing APIs
with recorded responses (``billing/ce-<payer>.json``, keyed by the requested
period, and ``billing/invoices-<payer>.json``), paged by ``page_size`` with
opaque tokens, 400 ``LimitExceededException`` on demand, and a validation
error for a period nothing was recorded for.
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
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from cloudops_connectors.aws.client import CE_TARGET, CONFIG_TARGET, INVOICING_TARGET

REGION: Final = "eu-west-1"
AGGREGATOR: Final = "getronics-org-aggregator"
CONFIG_HOST: Final = "https://config.fixture.example"
S3_HOST: Final = "https://cur.fixture.example"
S3_NS: Final = "http://s3.amazonaws.com/doc/2006-03-01/"
CE_HOST: Final = "https://ce.fixture.example"
INVOICING_HOST: Final = "https://invoicing.fixture.example"
BILLING_DAY: Final = "2026-10-28"  # the day the billing fixtures were recorded
_SINCE: Final = re.compile(r"configurationItemCaptureTime >= '([0-9T:.\-]+Z)'")


def load_fixture(name: str) -> bytes:
    return resources.files("cloudops_connectors.aws").joinpath(f"fixtures/{name}").read_bytes()


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class FakeConfig:
    def __init__(self, results: list[str]) -> None:
        self.results = list(results)
        self.throttle_next = 0
        self.fail_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls) -> FakeConfig:
        return cls(json.loads(load_fixture("config/results.json")))

    def touch(self, arn: str, captured: str, **fields: Any) -> None:
        """Change an item as Config would: new values, a later capture time."""
        for i, r in enumerate(self.results):
            try:
                item = json.loads(r)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("arn") == arn:
                item.update(fields, configurationItemCaptureTime=captured)
                self.results[i] = json.dumps(item, sort_keys=True)
                return
        raise KeyError(arn)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"Content-Type": "application/x-amz-json-1.1",
                   "x-amzn-RequestId": f"req-{len(self.requests):06d}"}
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(400, headers=headers, content=json.dumps({
                "__type": "ThrottlingException", "message": "Rate exceeded"}).encode())
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(500, headers=headers, content=b'{"__type":"InternalFailure"}')
        if request.method != "POST" or request.headers.get("x-amz-target") != CONFIG_TARGET:
            return httpx.Response(400, headers=headers,
                                  content=b'{"__type":"UnknownOperationException"}')
        body = json.loads(request.content)
        found = _SINCE.search(str(body.get("Expression", "")))
        since = _time(found.group(1)) if found else None
        matching = [r for r in self.results if self._after(r, since)]
        start = int(base64.b64decode(body["NextToken"])) if body.get("NextToken") else 0
        limit = int(body.get("Limit") or 100)
        page = matching[start:start + limit]
        out: dict[str, Any] = {
            "Results": page,
            "QueryInfo": {"SelectFields": [{"Name": f} for f in
                                           str(body.get("Expression", "")).split(" WHERE")[0]
                                           .removeprefix("SELECT ").split(", ")]},
        }
        if start + limit < len(matching):
            out["NextToken"] = base64.b64encode(str(start + limit).encode()).decode()
        return httpx.Response(200, headers=headers, content=json.dumps(out).encode())

    @staticmethod
    def _after(result: str, since: datetime | None) -> bool:
        try:
            item = json.loads(result)
            captured = _time(item["configurationItemCaptureTime"])
        except (ValueError, KeyError, TypeError, AttributeError):
            return True  # what the fake cannot read, it serves as it is
        return since is None or captured >= since


@dataclass(frozen=True)
class _Stored:
    file: str
    last_modified: datetime


class FakeS3:
    def __init__(self, bucket: str, stages: list[dict[str, _Stored]],
                 files: dict[str, bytes], reports: tuple[str, ...] = ()) -> None:
        self.bucket = bucket
        self.reports = reports  # the report prefixes the fixtures hold
        self.stages = stages
        self.files = files
        self.objects: dict[str, _Stored] = {}
        self.stage = -1
        self.throttle_next = 0
        self.requests: list[httpx.Request] = []
        self.advance()

    @classmethod
    def from_fixtures(cls) -> FakeS3:
        layout = json.loads(load_fixture("cur/stages.json"))
        stages = [{k: _Stored(v["file"], _time(v["last_modified"])) for k, v in s.items()}
                  for s in layout["stages"]]
        names = {o.file for s in stages for o in s.values()}
        return cls(layout["bucket"], stages, {n: load_fixture(f"cur/{n}") for n in names},
                   tuple(layout["reports"]))

    def advance(self) -> None:
        """The bucket as AWS leaves it at the next stage."""
        self.stage += 1
        self.objects.update(self.stages[self.stage])

    def body(self, key: str) -> bytes:
        """What GetObject returns for ``key``: CSV is served gzip-compressed."""
        name = self.objects[key].file
        data = self.files[name]
        return gzip.compress(data, mtime=0) if name.endswith(".csv") else data

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(503, content=b"<Error><Code>SlowDown</Code>"
                                               b"<Message>Please reduce your request rate."
                                               b"</Message></Error>")
        path = request.url.path
        if path == "/" and request.url.params.get("list-type") == "2":
            return self._list(request.url.params)
        key = path.removeprefix("/")
        if key not in self.objects:
            root = Element("Error")
            SubElement(root, "Code").text = "NoSuchKey"
            SubElement(root, "Message").text = "The specified key does not exist."
            SubElement(root, "Key").text = key
            return httpx.Response(404, headers={"Content-Type": "application/xml"},
                                  content=tostring(root, encoding="utf-8", xml_declaration=True))
        modified = format_datetime(self.objects[key].last_modified, usegmt=True)
        return httpx.Response(200, headers={"Last-Modified": modified,
                                            "Content-Type": "application/octet-stream"},
                              content=self.body(key))

    def _list(self, params: httpx.QueryParams) -> httpx.Response:
        prefix = params.get("prefix", "")
        limit = int(params.get("max-keys", "1000"))
        after = params.get("continuation-token")
        start_after = base64.b64decode(after).decode() if after else ""
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > start_after)
        page, rest = keys[:limit], keys[limit:]
        root = Element("ListBucketResult", xmlns=S3_NS)
        SubElement(root, "Name").text = self.bucket
        SubElement(root, "Prefix").text = prefix
        SubElement(root, "KeyCount").text = str(len(page))
        SubElement(root, "MaxKeys").text = str(limit)
        SubElement(root, "IsTruncated").text = "true" if rest else "false"
        for k in page:
            c = SubElement(root, "Contents")
            SubElement(c, "Key").text = k
            SubElement(c, "LastModified").text = (
                self.objects[k].last_modified.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            SubElement(c, "ETag").text = f'"{len(self.body(k)):032x}"'
            SubElement(c, "Size").text = str(len(self.body(k)))
            SubElement(c, "StorageClass").text = "STANDARD"
        if rest:
            SubElement(root, "NextContinuationToken").text = (
                base64.b64encode(page[-1].encode()).decode())
        return httpx.Response(200, headers={"Content-Type": "application/xml"},
                              content=tostring(root, encoding="utf-8", xml_declaration=True))


# --- billing: Cost Explorer and Invoicing -------------------------------------------------


def _offset(token: str | None) -> int:
    return int(base64.b64decode(token)) if token else 0


def _token(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


class _FakeJsonApi:
    target: str

    def __init__(self, payer: str) -> None:
        self.payer = payer
        self.throttle_next = 0
        self.page_size = 100
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"Content-Type": "application/x-amz-json-1.1",
                   "x-amzn-RequestId": f"req-{len(self.requests):06d}"}
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(400, headers=headers, content=json.dumps({
                "__type": "LimitExceededException", "message": "Rate exceeded"}).encode())
        if request.method != "POST" or request.headers.get("x-amz-target") != self.target:
            return httpx.Response(400, headers=headers,
                                  content=b'{"__type":"UnknownOperationException"}')
        return self._answer(json.loads(request.content), headers)

    def _answer(self, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        raise NotImplementedError

    @staticmethod
    def _invalid(headers: dict[str, str], message: str) -> httpx.Response:
        return httpx.Response(400, headers=headers, content=json.dumps(
            {"__type": "ValidationException", "message": message}).encode())


class FakeCostExplorer(_FakeJsonApi):
    target = CE_TARGET

    def __init__(self, payer: str, responses: dict[str, dict[str, Any]]) -> None:
        super().__init__(payer)
        self.responses = responses  # "<start>/<end>" -> GetCostAndUsage response

    @classmethod
    def from_fixtures(cls, payer: str) -> FakeCostExplorer:
        return cls(payer, json.loads(load_fixture(f"billing/ce-{payer}.json")))

    def _answer(self, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        period = body.get("TimePeriod") or {}
        recorded = self.responses.get(f"{period.get('Start')}/{period.get('End')}")
        if (body.get("Granularity"), body.get("Metrics")) != ("MONTHLY", ["UnblendedCost"]):
            return self._invalid(headers, "unexpected granularity or metrics")
        if recorded is None:
            return self._invalid(headers, "nothing recorded for this period")
        out = copy.deepcopy(recorded)
        if not isinstance(out.get("ResultsByTime"), list):
            return httpx.Response(200, headers=headers, content=json.dumps(out).encode())
        start = _offset(body.get("NextPageToken"))
        groups = [(i, g) for i, r in enumerate(out["ResultsByTime"]) for g in r["Groups"]]
        page = groups[start:start + self.page_size]
        for i, r in enumerate(out["ResultsByTime"]):
            r["Groups"] = [g for j, g in page if j == i]
        if start + self.page_size < len(groups):
            out["NextPageToken"] = _token(start + self.page_size)
        return httpx.Response(200, headers=headers, content=json.dumps(out).encode())


class FakeInvoicing(_FakeJsonApi):
    target = INVOICING_TARGET

    def __init__(self, payer: str, summaries: list[dict[str, Any]]) -> None:
        super().__init__(payer)
        self.summaries = summaries

    @classmethod
    def from_fixtures(cls, payer: str) -> FakeInvoicing:
        doc = json.loads(load_fixture(f"billing/invoices-{payer}.json"))
        return cls(payer, list(doc["InvoiceSummaries"]))

    def _answer(self, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        selector = body.get("Selector") or {}
        if selector != {"ResourceType": "ACCOUNT_ID", "Value": self.payer}:
            return self._invalid(headers, "the selector is not this payer")
        window = (body.get("Filter") or {}).get("TimeInterval") or {}
        lo, hi = window.get("StartDate"), window.get("EndDate")
        if not isinstance(lo, int) or not isinstance(hi, int):
            return self._invalid(headers, "TimeInterval is required")
        matching = [s for s in self.summaries if lo <= int(s["IssuedDate"]) <= hi]
        start = _offset(body.get("NextToken"))
        size = min(int(body.get("MaxResults") or 100), self.page_size)
        out: dict[str, Any] = {"InvoiceSummaries": matching[start:start + size]}
        if start + size < len(matching):
            out["NextToken"] = _token(start + size)
        return httpx.Response(200, headers=headers, content=json.dumps(out).encode())
