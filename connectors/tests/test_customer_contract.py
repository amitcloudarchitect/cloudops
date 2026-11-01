"""Contract tests for the customer-credential check and its clients (ADR 0027),
against recorded-shape fixtures (``recorded_customer``). No database, no cloud.

- SigV4 matches AWS's own worked example, and refuses a foreign host.
- The Azure check passes ADR 0003's three read roles and refuses anything else, a
  wider scope, the wrong directory or client, a rejected secret; it never names
  a subscription outside the credential, and every call is a GET or the listing.
- The AWS check passes ReadOnlyAccess with a Cost Explorer read policy and refuses
  an inline policy, write actions, root keys, the wrong account, a key mismatch.
- The secret is read by name at each use, and a rotation is picked up.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest

from cloudops_config import EnvFileSecretStore
from cloudops_connectors.aws import iam
from cloudops_connectors.aws.sigv4 import AccessKey, ForeignHostError, SigV4Auth, signature
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.core.identity import Binding, TargetOutOfScopeError, admit
from cloudops_connectors.customer import check
from cloudops_connectors.customer.credentials import (
    StoredCredential,
    StoredSecretCredential,
    aws_keys,
)

RECORDED = Path(__file__).parent / "recorded_customer"
ARM = "https://management.azure.com"
SUB = "cd12743c-f76e-4284-aa62-eb8bd576ac91"
DIRECTORY = UUID("2fa5e26d-5ad2-415f-88a2-771caca87cbe")
CLIENT = UUID("3c1f0b52-0000-4000-8000-00000000c11e")
OID = "6f1d2c3b-0000-4000-8000-0000000000aa"
ACCOUNT = "123456789012"
KEY_ID = "AKIAIOSFODNN7EXAMPLE"


def _read(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


def _credential(provider: str = "azure", **kw: Any) -> StoredCredential:
    base: dict[str, Any] = dict(
        id=UUID("5ca1ab1e-0000-4000-8000-000000000001"),
        tenant_id=UUID("0c0ffee0-0000-4000-8000-00000000a25b"), provider=provider,
        code="azsponsor", directory_id=DIRECTORY, client_id=CLIENT, access_key_id=None,
        account_ids=(SUB,), secret_names=("customer-azsponsor-azure-secret",),
        expires_at=datetime.now(UTC) + timedelta(days=60))
    if provider == "aws":
        base |= dict(code="awslab", directory_id=None, client_id=None, access_key_id=KEY_ID,
                     account_ids=(ACCOUNT,),
                     secret_names=("customer-awslab-aws-access-key-id",
                                   "customer-awslab-aws-secret-access-key"))
    return StoredCredential(**(base | kw))


def _jwt(**claims: Any) -> str:
    def part(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class _Token:
    def __init__(self, **claims: Any) -> None:
        self.claims = {"tid": str(DIRECTORY), "appid": str(CLIENT), "oid": OID} | claims

    async def get_token(self, *scopes: str, **_: Any) -> Any:
        class T:
            token = _jwt(**self.claims)
        return T()


class _Arm:
    """ARM as the check sees it: a listing, role assignments, role definitions."""

    def __init__(self, assignments: str = "azure_role_assignments_read_only.json",
                 subscriptions: bytes | None = None) -> None:
        self.assignments = assignments
        self.subscriptions = subscriptions or _read("azure_subscriptions.json")
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            if path == "/subscriptions":
                return httpx.Response(200, content=self.subscriptions)
            if path.endswith("/roleAssignments"):
                assert request.url.params["$filter"] == f"assignedTo('{OID}')"
                return httpx.Response(200, content=_read(self.assignments))
            if "/roleDefinitions/b24988ac" in path:
                return httpx.Response(200, content=_read("azure_role_definition_contributor.json"))
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return httpx.MockTransport(handle)


async def _azure(arm: _Arm, token: Any = None, credential: StoredCredential | None = None
                 ) -> check.Outcome:
    c = credential or _credential()
    client = AzureClient(ARM, transport=arm.transport(), allowed=frozenset(c.account_ids))
    try:
        return await check.check_azure(c, token or _Token(), client)
    finally:
        await client.aclose()


# --- SigV4 ------------------------------------------------------------------------


def test_sigv4_matches_the_published_example() -> None:
    request = httpx.Request("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
                            headers={"content-type":
                                     "application/x-www-form-urlencoded; charset=utf-8"})
    authorization, stamp = signature(
        AccessKey("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"), request,
        "us-east-1", "iam", datetime(2015, 8, 30, 12, 36, tzinfo=UTC))
    assert stamp == "20150830T123600Z"
    assert authorization == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")


def test_sigv4_never_signs_for_another_host_and_never_shows_the_secret() -> None:
    auth = SigV4Auth(lambda: AccessKey(KEY_ID, "s3cr3t"), "us-east-1", "sts", iam.STS)
    flow = auth.auth_flow(httpx.Request("POST", "https://evil.example/"))
    with pytest.raises(ForeignHostError):
        next(flow)
    assert "s3cr3t" not in repr(AccessKey(KEY_ID, "s3cr3t"))


# --- Azure ------------------------------------------------------------------------


async def test_the_three_read_roles_pass() -> None:
    arm = _Arm()
    outcome = await _azure(arm)
    assert (outcome.status, outcome.outcome) == ("passed", "read_only")
    assert outcome.detail["held"] == {SUB: ["Cost Management Reader", "Monitoring Reader",
                                            "Reader"]}
    assert {r.method for r in arm.requests} == {"GET"}  # reads only


async def test_a_write_role_inherited_through_a_group_is_refused_and_named() -> None:
    outcome = await _azure(_Arm("azure_role_assignments_contributor.json"))
    assert (outcome.status, outcome.outcome) == ("refused", "role_not_allowed")
    assert outcome.detail["roles"] == ["Contributor"]


async def test_reader_is_required() -> None:
    only_cost = json.loads(_read("azure_role_assignments_read_only.json"))
    only_cost["value"] = only_cost["value"][1:]  # Cost Management and Monitoring Reader

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/roleAssignments"):
            return httpx.Response(200, json=only_cost)
        return httpx.Response(200, content=_read("azure_subscriptions.json"))

    c = _credential()
    client = AzureClient(ARM, transport=httpx.MockTransport(handle), allowed=frozenset({SUB}))
    outcome = await check.check_azure(c, _Token(), client)
    await client.aclose()
    assert (outcome.status, outcome.outcome) == ("refused", "reader_missing")


async def test_a_credential_seeing_more_than_its_tenant_is_refused_without_naming_the_rest(
) -> None:
    listing = json.loads(_read("azure_subscriptions.json"))
    other = "99999999-0000-4000-8000-000000000999"
    listing["value"].append({**listing["value"][0], "subscriptionId": other,
                             "displayName": "Another customer"})
    arm = _Arm(subscriptions=json.dumps(listing).encode())
    outcome = await _azure(arm)
    assert (outcome.status, outcome.outcome) == ("refused", "scope_wider_than_tenant")
    assert outcome.detail == {"other_subscriptions": 1}
    assert all(other not in str(r.url) for r in arm.requests)


async def test_a_subscription_the_credential_cannot_see_is_refused() -> None:
    missing = "88888888-0000-4000-8000-000000000888"
    outcome = await _azure(_Arm(), credential=_credential(account_ids=(SUB, missing)))
    assert (outcome.status, outcome.outcome) == ("refused", "subscription_not_visible")
    assert outcome.detail["missing"] == [missing]


@pytest.mark.parametrize(("claims", "code"), [
    ({"tid": "00000000-0000-4000-8000-000000000000"}, "directory_mismatch"),
    ({"appid": "00000000-0000-4000-8000-000000000000"}, "client_mismatch"),
])
async def test_the_token_must_be_the_onboarded_directory_and_client(
    claims: dict[str, str], code: str,
) -> None:
    arm = _Arm()
    outcome = await _azure(arm, _Token(**claims))
    assert (outcome.status, outcome.outcome) == ("refused", code)
    assert arm.requests == []


async def test_a_rejected_secret_is_refused() -> None:
    from azure.core.exceptions import ClientAuthenticationError

    class Rejected:
        async def get_token(self, *scopes: str, **_: Any) -> Any:
            raise ClientAuthenticationError("AADSTS7000215: Invalid client secret provided.")

    outcome = await _azure(_Arm(), Rejected())
    assert (outcome.status, outcome.outcome) == ("refused", "credential_rejected")


async def test_the_check_client_is_allowed_exactly_the_credential() -> None:
    client = AzureClient(ARM, transport=_Arm().transport(),
                         allowed=frozenset({"77777777-0000-4000-8000-000000000777"}))
    with pytest.raises(ValueError, match="exactly"):
        await check.check_azure(_credential(), _Token(), client)
    with pytest.raises(TargetOutOfScopeError):
        await client.role_assignments(SUB, OID)
    await client.aclose()


def test_a_binding_admits_its_own_accounts_only() -> None:
    binding = Binding(UUID(int=1), frozenset({SUB}))
    assert admit(binding, [SUB, "x", SUB]) == [SUB]
    assert admit(None, ["b", "a"]) == ["a", "b"]
    with pytest.raises(ValueError, match="at least one account"):
        Binding(UUID(int=1), frozenset())


# --- AWS --------------------------------------------------------------------------


def _store(tmp_path: Path, key_id: str = KEY_ID) -> EnvFileSecretStore:
    path = tmp_path / ".env.local"
    path.write_text(f"CUSTOMER_AWSLAB_AWS_ACCESS_KEY_ID={key_id}\n"
                    "CUSTOMER_AWSLAB_AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIEXAMPLEKEY\n",
                    encoding="utf-8")
    return EnvFileSecretStore.from_file(path)


def _aws(responses: dict[str, str], seen: list[dict[str, list[str]]]
         ) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        seen.append(form)
        action = form["Action"][0]
        name = responses.get(action)
        if name is None:
            return httpx.Response(500)
        status = 403 if "error" in name else 200
        return httpx.Response(status, content=_read(name))
    return handle


IAM_OK: dict[str, str] = {
    "GetCallerIdentity": "sts_caller_identity.xml",
    "ListAttachedUserPolicies": "iam_list_attached_user_policies.xml",
    "ListUserPolicies": "iam_list_user_policies_empty.xml",
    "ListGroupsForUser": "iam_list_groups_for_user.xml",
    "ListAttachedGroupPolicies": "iam_list_attached_group_policies.xml",
    "ListGroupPolicies": "iam_list_group_policies_empty.xml",
    "GetPolicy": "iam_get_policy.xml",
    "GetPolicyVersion": "iam_get_policy_version_ce_read.xml",
}


async def _aws_check(tmp_path: Path, changes: dict[str, str] | None = None,
                     key_id: str = KEY_ID) -> tuple[check.Outcome, list[dict[str, list[str]]]]:
    seen: list[dict[str, list[str]]] = []
    transport = httpx.MockTransport(_aws(IAM_OK | (changes or {}), seen))
    sts = iam.QueryClient(iam.STS, iam.STS_VERSION, transport=transport)
    client = iam.QueryClient(iam.IAM, iam.IAM_VERSION, transport=transport)
    try:
        return await check.check_aws(_credential("aws"), _store(tmp_path, key_id), sts,
                                     client), seen
    finally:
        await sts.aclose()
        await client.aclose()


async def test_read_only_access_with_a_cost_explorer_read_policy_passes(tmp_path: Path) -> None:
    outcome, seen = await _aws_check(tmp_path)
    assert (outcome.status, outcome.outcome) == ("passed", "read_only")
    assert outcome.detail["held"] == ["Cost Explorer read (CloudOpsCostExplorerRead)",
                                      "ReadOnlyAccess"]
    assert {f["UserName"][0] for f in seen if "UserName" in f} == {"cloudops-read"}
    assert all(f["Action"][0].startswith(("Get", "List")) for f in seen)


@pytest.mark.parametrize(("changes", "code"), [
    ({"ListUserPolicies": "iam_list_user_policies_inline.xml"}, "inline_policy"),
    ({"GetPolicyVersion": "iam_get_policy_version_ce_write.xml"}, "policy_not_allowed"),
    ({"GetCallerIdentity": "sts_caller_identity_root.xml"}, "not_an_iam_user"),
    ({"GetCallerIdentity": "sts_error_invalid_token.xml"}, "credential_rejected"),
])
async def test_anything_beyond_read_is_refused(tmp_path: Path, changes: dict[str, str],
                                               code: str) -> None:
    outcome, _ = await _aws_check(tmp_path, changes)
    assert (outcome.status, outcome.outcome) == ("refused", code)


async def test_the_wrong_account_or_key_is_refused(tmp_path: Path) -> None:
    outcome, seen = await _aws_check(tmp_path, key_id="AKIAOTHERKEY00000000")
    assert (outcome.status, outcome.outcome, seen) == ("refused", "access_key_mismatch", [])
    other = _credential("aws", account_ids=("210987654321",))
    seen2: list[dict[str, list[str]]] = []
    transport = httpx.MockTransport(_aws(IAM_OK, seen2))
    sts = iam.QueryClient(iam.STS, iam.STS_VERSION, transport=transport)
    outcome = await check.check_aws(other, _store(tmp_path), sts, sts)
    await sts.aclose()
    assert (outcome.status, outcome.outcome) == ("refused", "account_mismatch")


def test_a_customer_managed_policy_passes_only_with_cost_explorer_reads() -> None:
    ok = {"Statement": [{"Effect": "Allow", "Action": ["ce:Get*", "ce:ListCostCategoryDefinitions"],
                         "Resource": "*"}]}
    assert check.ce_read_only(ok)
    bad: dict[str, Any]
    for bad in ({"Statement": [{"Effect": "Allow", "Action": "ce:*", "Resource": "*"}]},
                {"Statement": [{"Effect": "Allow", "NotAction": "ce:Get*", "Resource": "*"}]},
                {"Statement": [{"Effect": "Deny", "Action": "ce:Get*", "Resource": "*"}]},
                {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]},
                {"Statement": []}, {}):
        assert not check.ce_read_only(bad), bad


# --- the secret, by name ----------------------------------------------------------


def test_the_key_vault_key_id_must_be_the_onboarded_one(tmp_path: Path) -> None:
    assert aws_keys(_store(tmp_path), _credential("aws")).key_id == KEY_ID
    with pytest.raises(ValueError, match="not the one onboarded"):
        aws_keys(_store(tmp_path, "AKIAOTHERKEY00000000"), _credential("aws"))


async def test_the_azure_secret_is_read_at_each_token_and_a_rotation_is_picked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import azure.identity.aio as aio

    built: list[str] = []

    class Fake:
        def __init__(self, tenant: str, client: str, secret: str, **_: Any) -> None:
            built.append(secret)

        async def get_token(self, *scopes: str, **_: Any) -> str:
            return "token"

        async def close(self) -> None:
            return None

    monkeypatch.setattr(aio, "ClientSecretCredential", Fake)
    path = tmp_path / ".env.local"
    path.write_text("CUSTOMER_AZSPONSOR_AZURE_SECRET=first\n", encoding="utf-8")
    token = StoredSecretCredential(EnvFileSecretStore.from_file(path), _credential())
    await token.get_token("x")
    await token.get_token("x")
    assert built == ["first"]
    path.write_text("CUSTOMER_AZSPONSOR_AZURE_SECRET=second\n", encoding="utf-8")
    token._store = EnvFileSecretStore.from_file(path)  # the CSI driver refreshed the file
    await token.get_token("x")
    assert built == ["first", "second"]
    await token.close()
