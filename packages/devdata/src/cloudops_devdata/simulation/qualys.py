"""The simulation's findings in the Qualys connector's recorded-response format,
and a way to run the real step 15 connector over them.

    python -m cloudops_devdata.simulation qualys run --fixtures

Same shape as ``cloudops_devdata.qualys_fixtures``: a Host List VM Detection
response and the KnowledgeBase entries for its QIDs. Each host is a lab VM,
named as Azure names it, with its (simulated) private address and the customer
tag ``Customer:Azure Sponsorship``, which the connector resolves through the
tenant's ``qualys`` alias. Served by the connector's own fake subscription, so
the connector code that runs is exactly the step 15 code; nothing here writes a
finding itself.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Final
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from cloudops_devdata.qualys_fixtures import _kb, _text
from cloudops_devdata.simulation.model import NAMESPACE, QUALYS_TAG, Simulation
from cloudops_devdata.simulation.rows import PlatformRows

TIME: Final = "%Y-%m-%dT%H:%M:%SZ"


def _t(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIME)


def host_id(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"qualys:{name}").int % 10**9)


def detections(sim: Simulation, rows: PlatformRows) -> bytes:
    """Every finding in ``rows.vulnerabilities``, grouped by host."""
    vms = {v.name.lower(): v for v in sim.vms}
    root = Element("HOST_LIST_VM_DETECTION_OUTPUT")
    response = SubElement(root, "RESPONSE")
    scanned = datetime.combine(sim.anchor, time(4, 0), UTC)
    _text(response, "DATETIME", _t(scanned))
    host_list = SubElement(response, "HOST_LIST")
    by_host = defaultdict(list)
    for f in rows.vulnerabilities:
        by_host[f.asset_name].append(f)
    for name in sorted(by_host, key=host_id):
        vm, found = vms[name], by_host[name]
        host = SubElement(host_list, "HOST")
        _text(host, "ID", host_id(vm.name))
        _text(host, "IP", vm.private_ip)
        _text(host, "TRACKING_METHOD", "AGENT")
        _text(host, "OS", vm.os_version)
        _text(host, "DNS", vm.name.lower())
        dns = SubElement(host, "DNS_DATA")
        _text(dns, "HOSTNAME", vm.name.lower())
        _text(host, "NETBIOS", vm.name.upper()[:15])
        _text(host, "LAST_SCAN_DATETIME", _t(scanned))
        _text(host, "NETWORK_ID", "0")
        tags = SubElement(host, "TAGS")
        for i, tag in enumerate((QUALYS_TAG, f"RG:{vm.resource_group}")):
            t = SubElement(tags, "TAG")
            _text(t, "TAG_ID", 20_000 + i)
            _text(t, "NAME", tag)
        listing = SubElement(host, "DETECTION_LIST")
        for f in found:
            d = SubElement(listing, "DETECTION")
            _text(d, "QID", f.qid)
            _text(d, "TYPE", f.detection_type)
            _text(d, "SEVERITY", f.severity)
            _text(d, "STATUS", "Fixed" if f.remediated_at else "Active")
            _text(d, "FIRST_FOUND_DATETIME", _t(f.first_detected))
            _text(d, "LAST_FOUND_DATETIME", _t(f.last_detected))
            if f.remediated_at:
                _text(d, "LAST_FIXED_DATETIME", _t(f.remediated_at))
            _text(d, "LAST_PROCESSED_DATETIME", _t(f.last_detected + timedelta(minutes=7)))
            _text(d, "TIMES_FOUND", max(1, (f.last_detected - f.first_detected).days))
            _text(d, "RESULTS", "Detected by the cloud agent.")
    indent(root, space=" ")
    return bytes(tostring(root, encoding="utf-8", xml_declaration=True)) + b"\n"


def knowledge_base() -> bytes:
    """The KnowledgeBase entries for every QID the simulation can use."""
    return _kb()
