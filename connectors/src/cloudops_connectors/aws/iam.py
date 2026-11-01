"""STS and IAM reads for checking a customer's access key (ADR 0003, ADR 0027).

    sts = QueryClient(STS, auth=SigV4Auth(keys, "us-east-1", "sts", STS))
    who = await caller_identity(sts)
    iam = QueryClient(IAM, auth=SigV4Auth(keys, "us-east-1", "iam", IAM))
    grants = await user_grants(iam, who.user_name)

Read-only calls, all covered by ``ReadOnlyAccess``: ``GetCallerIdentity``, and for
the key's own user ``ListAttachedUserPolicies``, ``ListUserPolicies``,
``ListGroupsForUser``, each group's ``ListAttachedGroupPolicies`` and
``ListGroupPolicies``, and for a customer-managed policy ``GetPolicy`` and
``GetPolicyVersion``. Responses are XML, parsed with ``defusedxml``
(``core/xmlrec.py``); pagination (``IsTruncated`` / ``Marker``) is followed.

Nothing here decides what is allowed: ``customer/check.py`` does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import unquote
from xml.etree.ElementTree import Element

import httpx

from cloudops_connectors.core import xmlrec

STS: Final = "https://sts.amazonaws.com"
IAM: Final = "https://iam.amazonaws.com"
STS_VERSION: Final = "2011-06-15"
IAM_VERSION: Final = "2010-05-08"
_USER_ARN: Final = re.compile(r"arn:aws:iam::(?P<account>[0-9]{12}):user/(?:[\w+=,.@/-]*/)?"
                              r"(?P<name>[\w+=,.@-]{1,64})")
MAX_PAGES: Final = 50


class AwsRefusedError(Exception):
    """AWS answered 4xx: the key is unknown, wrong or not permitted. ``code`` is
    AWS's error code (``InvalidClientTokenId``, ``AccessDenied``...), never a key."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"{code} ({status})")
        self.code = code
        self.status = status


def _strip(element: Element) -> Element:
    for e in element.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return element


def _error_code(body: bytes) -> str:
    try:
        root = _strip(xmlrec.parse(body))
    except xmlrec.MalformedXmlError:
        return "Unknown"
    code = root.find(".//Error/Code")
    text = (code.text or "").strip() if code is not None else ""
    return text if re.fullmatch(r"[A-Za-z.]{1,64}", text) else "Unknown"


class QueryClient:
    """One AWS Query-protocol service (STS or IAM), signed by the auth passed in."""

    def __init__(self, base_url: str, version: str, *, auth: httpx.Auth | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._version = version
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, transport=transport,
                                       timeout=60.0)

    async def call(self, action: str, **params: str) -> dict[str, Any]:
        """The action's ``<Action>Result`` as a record."""
        form = {"Action": action, "Version": self._version, **params}
        response = await self._http.post(
            "/", data=form,
            headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"})
        if 400 <= response.status_code < 500:
            raise AwsRefusedError(_error_code(response.content), response.status_code)
        response.raise_for_status()
        root = _strip(xmlrec.parse(response.content))
        result = root.find(f"{action}Result")
        if result is None:
            raise ValueError(f"{action}: no result in the response")
        out = xmlrec.record(result)
        return out if isinstance(out, dict) else {}

    async def pages(self, action: str, member: str, **params: str) -> list[Any]:
        """Every ``member`` of a paginated list, following ``Marker``."""
        items: list[Any] = []
        marker: str | None = None
        for _ in range(MAX_PAGES):
            extra = {} if marker is None else {"Marker": marker}
            result = await self.call(action, **params, **extra)
            listed = result.get(member)
            items.extend(xmlrec.as_list(listed.get("member") if isinstance(listed, dict)
                                        else None))
            if str(result.get("IsTruncated", "false")).lower() != "true":
                return items
            marker = str(result.get("Marker", "")) or None
            if marker is None:
                raise ValueError(f"{action}: truncated with no marker")
        raise ValueError(f"{action}: more than {MAX_PAGES} pages")

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass(frozen=True)
class CallerIdentity:
    account: str
    arn: str
    user_name: str | None  # None: not an IAM user (the root user, an assumed role)


async def caller_identity(sts: QueryClient) -> CallerIdentity:
    result = await sts.call("GetCallerIdentity")
    account, arn = str(result.get("Account", "")), str(result.get("Arn", ""))
    found = _USER_ARN.fullmatch(arn)
    return CallerIdentity(account, arn, found["name"] if found else None)


@dataclass(frozen=True)
class Policy:
    arn: str
    name: str
    holder: str  # "user" or "group:<name>"


@dataclass(frozen=True)
class Grants:
    attached: list[Policy]
    inline: list[str]  # "user:<policy>" or "group:<group>/<policy>"


async def user_grants(iam: QueryClient, user: str) -> Grants:
    attached = [Policy(str(p.get("PolicyArn", "")), str(p.get("PolicyName", "")), "user")
                for p in await iam.pages("ListAttachedUserPolicies", "AttachedPolicies",
                                         UserName=user)]
    inline = [f"user:{n}" for n in await iam.pages("ListUserPolicies", "PolicyNames",
                                                   UserName=user)]
    for group in await iam.pages("ListGroupsForUser", "Groups", UserName=user):
        name = str(group.get("GroupName", ""))
        attached += [Policy(str(p.get("PolicyArn", "")), str(p.get("PolicyName", "")),
                            f"group:{name}")
                     for p in await iam.pages("ListAttachedGroupPolicies", "AttachedPolicies",
                                              GroupName=name)]
        inline += [f"group:{name}/{n}" for n in await iam.pages(
            "ListGroupPolicies", "PolicyNames", GroupName=name)]
    return Grants(attached, inline)


async def policy_document(iam: QueryClient, arn: str) -> dict[str, Any]:
    """A customer-managed policy's default version, as JSON."""
    policy = (await iam.call("GetPolicy", PolicyArn=arn)).get("Policy", {})
    version = str(policy.get("DefaultVersionId", "")) if isinstance(policy, dict) else ""
    if not re.fullmatch(r"v[0-9]{1,5}", version):
        raise ValueError("a policy without a default version")
    got = (await iam.call("GetPolicyVersion", PolicyArn=arn, VersionId=version)).get(
        "PolicyVersion", {})
    document = got.get("Document", "") if isinstance(got, dict) else ""
    parsed = json.loads(unquote(str(document)))
    if not isinstance(parsed, dict):
        raise ValueError("a policy document is a JSON object")
    return parsed
