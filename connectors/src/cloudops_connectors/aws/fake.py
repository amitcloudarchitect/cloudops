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
"""

from __future__ import annotations

import base64
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

from cloudops_connectors.aws.client import CONFIG_TARGET

REGION: Final = "eu-west-1"
AGGREGATOR: Final = "getronics-org-aggregator"
CONFIG_HOST: Final = "https://config.fixture.example"
S3_HOST: Final = "https://cur.fixture.example"
S3_NS: Final = "http://s3.amazonaws.com/doc/2006-03-01/"
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
