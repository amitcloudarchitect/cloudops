"""A fake Qualys subscription serving recorded fixtures through httpx.

    fake = FakeQualys.from_fixtures()
    client = QualysClient(FIXTURE_HOST, transport=fake.transport())

It answers the two APIs as the client uses them: detections filtered by
``vm_processed_after`` (per detection, as Qualys does), hosts in id order from
``id_min``, ``truncation_limit`` with the truncation WARNING and its resume URL,
and the KnowledgeBase by ``ids``; plus the rate-limit headers and 409 with
``X-RateLimit-ToWait-Sec``. Tests can edit a detection (``touch``) or force
throttling and failures.
"""

from __future__ import annotations

import copy
from datetime import datetime
from importlib import resources
from typing import Final
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from cloudops_connectors.core.xmlrec import parse
from cloudops_connectors.qualys.client import DETECTION_PATH, KB_PATH

FIXTURE_HOST: Final = "https://qualysapi.fixture.example"


def load_fixture(name: str) -> bytes:
    return resources.files("cloudops_connectors.qualys").joinpath(f"fixtures/{name}").read_bytes()


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class FakeQualys:
    def __init__(self, detections: bytes, kb: bytes, *, quota: int = 300) -> None:
        self.hosts = list(parse(detections).iterfind("RESPONSE/HOST_LIST/HOST"))
        self.vulns = list(parse(kb).iterfind("RESPONSE/VULN_LIST/VULN"))
        self.quota = quota
        self.remaining = quota
        self.throttle_next = 0
        self.fail_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls, **kwargs: int) -> FakeQualys:
        return cls(load_fixture("host_detection.xml"), load_fixture("knowledge_base.xml"),
                   **kwargs)

    def touch(self, host_id: str, qid: int, processed: str, **fields: str) -> None:
        """Edit a detection as Qualys would: new values, a later processed time."""
        for host in self.hosts:
            if host.findtext("ID") != host_id:
                continue
            for d in host.iterfind("DETECTION_LIST/DETECTION"):
                if d.findtext("QID") == str(qid):
                    for name, value in {**fields, "LAST_PROCESSED_DATETIME": processed}.items():
                        el = d.find(name)
                        if el is None:
                            el = SubElement(d, name)
                        el.text = value
                    return
        raise KeyError((host_id, qid))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.quota),
            "X-RateLimit-Window-Sec": "3600",
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-Concurrency-Limit-Limit": "1",
            "X-Concurrency-Limit-Running": "0",
            "Content-Type": "text/xml;charset=UTF-8",
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(409, headers={**self._headers(),
                                                "X-RateLimit-ToWait-Sec": "11"},
                                  content=b"<SIMPLE_RETURN><RESPONSE><CODE>1965</CODE>"
                                          b"</RESPONSE></SIMPLE_RETURN>")
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, content=b"unavailable")
        self.remaining -= 1
        params = request.url.params
        if request.url.path == DETECTION_PATH and params.get("action") == "list":
            return httpx.Response(200, headers=self._headers(), content=self._detections(params))
        if request.url.path == KB_PATH and params.get("action") == "list":
            return httpx.Response(200, headers=self._headers(), content=self._kb(params))
        return httpx.Response(404, content=b"not found")

    def _detections(self, params: httpx.QueryParams) -> bytes:
        since = _time(params.get("vm_processed_after"))
        id_min = int(params.get("id_min", "0"))
        limit = int(params.get("truncation_limit", "1000"))
        matching: list[Element] = []
        for host in sorted(self.hosts, key=lambda h: int(h.findtext("ID") or "0")):
            if int(host.findtext("ID") or "0") < id_min:
                continue
            h = copy.deepcopy(host)
            dl = h.find("DETECTION_LIST")
            if dl is not None and since is not None:
                for d in list(dl):
                    processed = _time(d.findtext("LAST_PROCESSED_DATETIME"))
                    if processed is not None and processed <= since:
                        dl.remove(d)
                if len(dl) == 0:
                    continue
            matching.append(h)
        page, rest = matching[:limit], matching[limit:]
        root = Element("HOST_LIST_VM_DETECTION_OUTPUT")
        response = SubElement(root, "RESPONSE")
        SubElement(response, "DATETIME").text = "2026-10-27T00:00:00Z"
        host_list = SubElement(response, "HOST_LIST")
        host_list.extend(page)
        if rest:
            warning = SubElement(response, "WARNING")
            SubElement(warning, "CODE").text = "1980"
            SubElement(warning, "TEXT").text = f"{limit} record limit exceeded."
            SubElement(warning, "URL").text = (
                f"{FIXTURE_HOST}{DETECTION_PATH}?action=list&truncation_limit={limit}"
                f"&id_min={rest[0].findtext('ID')}")
        return bytes(tostring(root, encoding="utf-8", xml_declaration=True))

    def _kb(self, params: httpx.QueryParams) -> bytes:
        wanted = set(params.get("ids", "").split(","))
        root = Element("KNOWLEDGE_BASE_VULN_LIST_OUTPUT")
        vl = SubElement(SubElement(root, "RESPONSE"), "VULN_LIST")
        vl.extend(copy.deepcopy(v) for v in self.vulns if v.findtext("QID") in wanted)
        return bytes(tostring(root, encoding="utf-8", xml_declaration=True))
