"""The read-only check of a stored customer credential (ADR 0003, guardrails 1 and 5).

Run by the connector worker, which alone holds the secret, when an onboarding, a
rotation or a recheck asks for it. Every call is a read. The outcome is one of:

- ``passed``: the credential works, reaches exactly the accounts it was onboarded
  for, and holds only the roles ADR 0003 allows;
- ``refused``: it does not, and must not be used (``outcome`` says why, as a code);
- ``failed``: the cloud did not answer; nothing is decided, ask again.

**Azure.** A token from the app registration's secret (refused: ``credential_
rejected``). Its claims must name the onboarded directory and client. The
subscription listing must show every onboarded subscription and nothing else: a
credential that sees more than its tenant's subscriptions is refused
(``scope_wider_than_tenant``), so one credential reaches one tenant. On each
subscription, every role assignment the principal holds, directly or through a
group, at, above or below it, must be one of ADR 0003's three built-in roles,
matched by their fixed definition ids: Reader, Cost Management Reader, Monitoring
Reader. Anything else, a custom role included, is refused and named
(``role_not_allowed``). Reader must be among them (``reader_missing``): without it
nothing can be read. Two of the allowed built-ins carry actions beyond ``read``
(``Microsoft.Support/*`` and a Log Analytics search): ADR 0003 names those roles,
and the connectors call neither.

**AWS.** The key id in Key Vault must be the one onboarded. ``GetCallerIdentity``
must name the onboarded account and an IAM user (never the root user's keys).
The user's policies, its groups' included, must be ``ReadOnlyAccess``, optionally
``AWSBillingReadOnlyAccess``, or a customer-managed policy granting only Cost
Explorer reads (``ce:Get*``, ``ce:Describe*``, ``ce:List*``, ``Allow`` only). An inline
policy, or any other policy, is refused. ``ReadOnlyAccess`` must be there.

Not checkable with read roles, so left to the written instructions
(docs/cloud-onboarding.md): Entra directory roles and Graph permissions on the
app registration, and AWS permission boundaries and SCPs (which only narrow).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx

from cloudops_config import SecretStore
from cloudops_connectors.aws import iam
from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.auth import ARM_SCOPE, TokenCredential
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.customer.credentials import (
    KeyMismatchError,
    StoredCredential,
    aws_keys,
)

Status = Literal["passed", "refused", "failed"]

# ADR 0003, guardrail 1: the built-in roles' definition ids, the same in every directory.
READER: Final = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
AZURE_ALLOWED: Final = {
    READER: "Reader",
    "72fafb9e-0641-4937-9268-a91bfd8191a3": "Cost Management Reader",
    "43d0d8ad-25c7-4714-9337-8ba259a9fe05": "Monitoring Reader",
}
READ_ONLY_ACCESS: Final = "arn:aws:iam::aws:policy/ReadOnlyAccess"
AWS_ALLOWED: Final = {
    READ_ONLY_ACCESS: "ReadOnlyAccess",
    "arn:aws:iam::aws:policy/AWSBillingReadOnlyAccess": "AWSBillingReadOnlyAccess",
}
_CE_READ: Final = re.compile(r"ce:(Get|Describe|List)[A-Za-z]*\*?")
MAX_LISTING_PAGES: Final = 20


@dataclass(frozen=True)
class Outcome:
    status: Status
    outcome: str  # a code: [a-z_]{1,60}
    detail: dict[str, Any] = field(default_factory=dict)


def _refused(code: str, **detail: Any) -> Outcome:
    return Outcome("refused", code, detail)


def _failed(code: str, **detail: Any) -> Outcome:
    return Outcome("failed", code, detail)


def claims(token: str) -> dict[str, Any]:
    """An Entra access token's claims, unverified: the token came from Entra over
    TLS a moment ago, and only identifiers are read from it. Never logged."""
    try:
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _tail(definition_id: str) -> str:
    return definition_id.rstrip("/").rsplit("/", 1)[-1].lower()


async def _listing(client: AzureClient) -> set[str]:
    seen: set[str] = set()
    link: str | None = None
    for _ in range(MAX_LISTING_PAGES):
        body = await client.subscriptions(link)
        seen |= {s.subscription_id for s in mapping.subscriptions(body)}
        link = mapping.next_link(body)
        if link is None:
            return seen
    raise ValueError("the subscription listing did not end")


async def _role_name(client: AzureClient, definition_id: str) -> str:
    try:
        body = json.loads(await client.role_definition(definition_id))
    except (ValueError, httpx.HTTPStatusError):
        return f"role {_tail(definition_id)}"  # defined above the subscription
    name = (body.get("properties") or {}).get("roleName") if isinstance(body, dict) else None
    return str(name)[:100] if isinstance(name, str) and name.strip() else (
        f"role {_tail(definition_id)}")


async def check_azure(credential: StoredCredential, token: TokenCredential,
                      client: AzureClient) -> Outcome:
    """``client`` must be allowed exactly the credential's subscriptions."""
    from azure.core.exceptions import ClientAuthenticationError

    if client.allowed != frozenset(credential.account_ids):
        raise ValueError("the check's client is allowed exactly the credential's subscriptions")
    try:
        got = claims((await token.get_token(ARM_SCOPE)).token)
    except ClientAuthenticationError:
        return _refused("credential_rejected")
    except (httpx.TransportError, OSError):
        return _failed("cloud_unreachable")
    if str(got.get("tid", "")).lower() != str(credential.directory_id):
        return _refused("directory_mismatch")
    if str(got.get("appid") or got.get("azp") or "").lower() != str(credential.client_id):
        return _refused("client_mismatch")
    principal = str(got.get("oid", "")).lower()
    try:
        visible = await _listing(client)
        recorded = set(credential.account_ids)
        if recorded - visible:
            return _refused("subscription_not_visible", missing=sorted(recorded - visible))
        if visible - recorded:
            # Counted, never named: another tenant's ids are not this record's.
            return _refused("scope_wider_than_tenant", other_subscriptions=len(visible - recorded))
        held: dict[str, list[str]] = {}
        refused: set[str] = set()
        for sub in sorted(recorded):
            body = json.loads(await client.role_assignments(sub, principal))
            names: set[str] = set()
            for a in body.get("value") or []:
                definition = str(((a or {}).get("properties") or {}).get("roleDefinitionId", ""))
                allowed = AZURE_ALLOWED.get(_tail(definition))
                if allowed is not None:
                    names.add(allowed)
                else:
                    refused.add(await _role_name(client, definition))
            held[sub] = sorted(names)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            return _refused("credential_rejected")
        if status == 403:
            return _refused("cannot_read_role_assignments")
        return _failed("cloud_error", status=status)
    except (httpx.TransportError, OSError):
        return _failed("cloud_unreachable")
    if refused:
        return _refused("role_not_allowed", roles=sorted(refused), held=held)
    without_reader = sorted(s for s, names in held.items() if "Reader" not in names)
    if without_reader:
        return _refused("reader_missing", subscriptions=without_reader, held=held)
    not_granted = sorted({n for n in AZURE_ALLOWED.values()} - {n for v in held.values()
                                                               for n in v})
    return Outcome("passed", "read_only", {"subscriptions": len(recorded), "held": held,
                                          "not_granted": not_granted})


def ce_read_only(document: Mapping[str, Any]) -> bool:
    """Allow statements naming only Cost Explorer reads, and nothing else."""
    statements = document.get("Statement")
    items: list[Any] = statements if isinstance(statements, list) else [statements]
    if not items or any(not isinstance(s, dict) for s in items):
        return False
    for s in items:
        if s.get("Effect") != "Allow" or "NotAction" in s or "NotResource" in s:
            return False
        actions = s.get("Action")
        listed = actions if isinstance(actions, list) else [actions]
        if not listed or any(not isinstance(a, str) or not _CE_READ.fullmatch(a)
                             for a in listed):
            return False
    return True


async def check_aws(credential: StoredCredential, store: SecretStore, sts: iam.QueryClient,
                    iam_client: iam.QueryClient) -> Outcome:
    """``sts`` and ``iam_client`` are signed with the credential's own key."""
    try:
        aws_keys(store, credential)
    except KeyMismatchError:
        return _refused("access_key_mismatch")
    account = credential.account_ids[0]
    try:
        who = await iam.caller_identity(sts)
        if who.account != account:
            return _refused("account_mismatch")
        if who.user_name is None:
            return _refused("not_an_iam_user")
        grants = await iam.user_grants(iam_client, who.user_name)
        if grants.inline:
            return _refused("inline_policy", policies=sorted(grants.inline)[:20])
        names: list[str] = []
        refused: list[str] = []
        for p in grants.attached:
            if p.arn in AWS_ALLOWED:
                names.append(AWS_ALLOWED[p.arn])
            elif (p.arn.startswith(f"arn:aws:iam::{account}:policy/")
                  and ce_read_only(await iam.policy_document(iam_client, p.arn))):
                names.append(f"Cost Explorer read ({p.name[:80]})")
            else:
                refused.append(p.name[:100] or p.arn[:100])
    except iam.AwsRefusedError as exc:
        if exc.code in {"InvalidClientTokenId", "SignatureDoesNotMatch", "ExpiredToken"}:
            return _refused("credential_rejected")
        if exc.code in {"AccessDenied", "AccessDeniedException"}:
            return _refused("cannot_read_own_policies")
        return _failed("cloud_error", aws_error=exc.code)
    except httpx.HTTPStatusError as exc:
        return _failed("cloud_error", status=exc.response.status_code)
    except (httpx.TransportError, OSError):
        return _failed("cloud_unreachable")
    if refused:
        return _refused("policy_not_allowed", policies=sorted(refused))
    if "ReadOnlyAccess" not in names:
        return _refused("read_only_access_missing", held=sorted(names))
    return Outcome("passed", "read_only", {"account": account, "held": sorted(set(names))})
