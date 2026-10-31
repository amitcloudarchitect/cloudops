"""XML payloads as plain records, parsed safely.

Qualys and S3 answer in XML. ``defusedxml`` refuses entity expansion and
external entities: a payload is attacker-influenceable data (CLAUDE.md rule 4)
and must not be able to make the parser do anything.

``record(element)`` turns an element into a dict: a leaf is its text, a
repeated child becomes a list, a nested element a dict. Field names stay
exactly as the source sent them.
"""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree


class MalformedXmlError(ValueError):
    """The payload is not well-formed XML, or tries something the safe parser refuses."""


def parse(body: bytes) -> Element:
    try:
        return ElementTree.fromstring(body)
    except Exception as exc:  # ParseError, or defusedxml's refusals
        raise MalformedXmlError(type(exc).__name__) from None


def record(element: Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    out: dict[str, Any] = {}
    for child in children:
        value = record(child)
        if child.tag in out:
            existing = out[child.tag]
            out[child.tag] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            out[child.tag] = value
    return out


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]
