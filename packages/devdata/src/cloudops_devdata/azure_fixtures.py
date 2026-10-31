"""Recorded Azure fixtures, built from the synthetic dataset.

    python -m cloudops_devdata.azure_fixtures      # rewrites the fixture files

An ARM subscription listing (the Lighthouse-delegated subscriptions) and the
Resource Graph rows for them, anonymised by construction. Built from
``synthetic.generate(FIXTURE_ANCHOR, FIXTURE_SEED)``, the dataset the connector
integration tests load: every Azure account, and every resource in it that is
not deleted, as Resource Graph reports it (type lowercased, power state as a
code, tags verbatim, including the one carrying an instruction).

Added on purpose: a subscription nobody has mapped (``UNREGISTERED``, with
resources of its own: quarantine), two rows without their keys (rejected), a
resource type the mapping does not know (``other``), and a row carrying a
field the mapping does not (``zones``, kept in raw_extra).

``expectations()`` is the ground truth per row, computed from the dataset
rather than from the connector. The committed files must equal ``build()``
exactly (a test checks).
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR, FIXTURE_SEED
from cloudops_devdata.synthetic import NAMESPACE, AccountRow, generate

FIXTURE_DIR: Final = (
    Path(__file__).resolve().parents[4]
    / "connectors/src/cloudops_connectors/azure/fixtures"
)
UNREGISTERED: Final = str(uuid.uuid5(NAMESPACE, "azure:subscription:unregistered"))
MANAGING_TENANT: Final = str(uuid.uuid5(NAMESPACE, "azure:directory:getronics"))
_POWER: Final = frozenset({"running", "stopped", "deallocated"})


@dataclass(frozen=True)
class Expected:
    native_id: str | None
    subscription_id: str
    tenant_code: str | None  # None: the subscription is quarantined
    resource_id: uuid.UUID | None = None  # the synthetic resource it is, if any
    name: str | None = None
    type_native: str | None = None
    type_canonical: str | None = None
    region: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    operating_system: str | None = None
    state: str | None = None
    malformed: bool = False


def _directory(code: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"azure:directory:{code}"))


def _os_type(os_name: str | None) -> str:
    if not os_name:
        return ""
    return "Windows" if "windows" in os_name.lower() else "Linux"


def _row(
    native_id: str, name: str, type_native: str, sub: str, directory: str, region: str,
    tags: dict[str, str], os_name: str | None, state: str,
) -> dict[str, Any]:
    group = native_id.split("/resourceGroups/", 1)[-1].split("/", 1)[0]
    return {
        "id": native_id,
        "name": name,
        "type": type_native.lower(),
        "location": region,
        "tags": tags or None,
        "subscriptionId": sub,
        "resourceGroup": group,
        "tenantId": directory,
        "kind": "",
        "sku": None,
        "osName": os_name or "",
        "osType": _os_type(os_name),
        "powerState": f"PowerState/{state}" if state in _POWER else "",
        "provisioningState": "Succeeded",
    }


def _subscription(sub: str, name: str, directory: str) -> dict[str, Any]:
    return {
        "id": f"/subscriptions/{sub}",
        "authorizationSource": "RoleBased",
        "managedByTenants": [{"tenantId": MANAGING_TENANT}],
        "subscriptionId": sub,
        "tenantId": directory,
        "displayName": name,
        "state": "Enabled",
        "subscriptionPolicies": {"locationPlacementId": "Public_2014-09-01",
                                 "quotaId": "EnterpriseAgreement_2014-09-01",
                                 "spendingLimit": "Off"},
    }


def _dump(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode()


def build() -> tuple[bytes, bytes, list[Expected]]:
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    code = {t.id: t.short_code for t in ds.tenants}
    accounts: dict[uuid.UUID, AccountRow] = {
        a.id: a for a in ds.accounts if a.provider == "azure"}
    subs = [_subscription(a.native_id, a.display_name, _directory(code[a.tenant_id]))
            for a in sorted(accounts.values(), key=lambda a: a.native_id)]
    rows: list[dict[str, Any]] = []
    expected: list[Expected] = []
    for r in ds.resources:
        acct = accounts.get(r.account_id)
        if acct is None or r.deleted_at is not None:
            continue
        rows.append(_row(r.native_id, r.name, r.type_native, acct.native_id,
                         _directory(code[r.tenant_id]), r.region, r.tags,
                         r.operating_system, r.state))
        expected.append(Expected(
            r.native_id, acct.native_id, code[r.tenant_id], r.id, r.name, r.type_native,
            r.type_canonical, r.region, r.tags, r.operating_system, r.state))

    nwl = next(a for a in sorted(accounts.values(), key=lambda a: a.native_id)
               if code[a.tenant_id] == "NWL" and a.environment == "prod")
    nwl_dir = _directory("NWL")
    base = f"/subscriptions/{nwl.native_id}/resourceGroups/rg-nwl-prod/providers"

    # A type the mapping does not know: kept, typed "other".
    vault = f"{base}/Microsoft.KeyVault/vaults/kv-nwl-weu-01"
    rows.append(_row(vault, "kv-nwl-weu-01", "Microsoft.KeyVault/vaults", nwl.native_id,
                     nwl_dir, "westeurope", {"environment": "prod"}, None, "Succeeded"))
    expected.append(Expected(vault, nwl.native_id, "NWL", None, "kv-nwl-weu-01",
                             "microsoft.keyvault/vaults", "other", "westeurope",
                             {"environment": "prod"}, None, "Succeeded"))
    # A field the mapping does not carry: kept in raw_extra.
    zonal = f"{base}/Microsoft.Compute/virtualMachines/nwl-weu-zonal-01"
    row = _row(zonal, "nwl-weu-zonal-01", "Microsoft.Compute/virtualMachines", nwl.native_id,
               nwl_dir, "westeurope", {}, "Ubuntu 22.04", "running")
    rows.append({**row, "zones": ["2"]})
    expected.append(Expected(zonal, nwl.native_id, "NWL", None, "nwl-weu-zonal-01",
                             "Microsoft.Compute/virtualMachines", "compute.vm", "westeurope",
                             {}, "Ubuntu 22.04", "running"))
    # Rows without their keys: rejected and counted, never guessed.
    untyped = {**_row(f"{base}/Microsoft.Web/sites/nwl-untyped", "nwl-untyped",
                      "Microsoft.Web/sites", nwl.native_id, nwl_dir, "westeurope", {}, None,
                      "Succeeded"), "type": None}
    rows.append(untyped)
    expected.append(Expected(untyped["id"], nwl.native_id, "NWL", malformed=True))
    no_id = {**_row(f"{base}/Microsoft.Web/sites/nwl-no-id", "nwl-no-id",
                    "Microsoft.Web/sites", nwl.native_id, nwl_dir, "westeurope", {}, None,
                    "Succeeded"), "id": ""}
    rows.append(no_id)
    expected.append(Expected(None, nwl.native_id, "NWL", malformed=True))

    # A subscription delegated to Getronics that nobody has mapped to a tenant.
    stray = _directory("unmapped")
    subs.append(_subscription(UNREGISTERED, "Woodgrove Sandbox", stray))
    for i, (kind, os_name) in enumerate((
            ("Microsoft.Compute/virtualMachines", "Windows Server 2022 Datacenter"),
            ("Microsoft.Compute/virtualMachines", "Ubuntu 22.04"),
            ("Microsoft.Storage/storageAccounts", None))):
        name = f"wgb-sandbox-{i:02d}"
        native = (f"/subscriptions/{UNREGISTERED}/resourceGroups/rg-sandbox/providers/"
                  f"{kind}/{name}")
        rows.append(_row(native, name, kind, UNREGISTERED, stray, "northeurope",
                         {"owner": "sandbox"}, os_name, "running"))
        expected.append(Expected(native, UNREGISTERED, None, None, name, kind))

    rows.sort(key=lambda r: str(r.get("id") or ""))
    listing = _dump({"value": subs, "count": {"type": "Total", "value": len(subs)}})
    graph = _dump({"totalRecords": len(rows), "count": len(rows), "data": rows,
                   "facets": [], "resultTruncated": "false"})
    return listing, graph, expected


def expectations() -> list[Expected]:
    return build()[2]


def main() -> int:
    listing, graph, expected = build()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "subscriptions.json").write_bytes(listing)
    (FIXTURE_DIR / "resources.json").write_bytes(graph)
    print(f"wrote {FIXTURE_DIR} ({len(expected)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
