"""Azure Resource Graph row -> ``cloudops.resource`` row; ARM subscription -> account.

Two payloads, both JSON:

- ARM ``GET /subscriptions``: the subscriptions Getronics is delegated through
  Lighthouse. Each ``subscriptionId`` is the customer key: an exact alias of
  source ``azure`` (identity.resolve_ids), never a display name.
- Resource Graph ``POST /providers/Microsoft.ResourceGraph/resources``: one row
  per resource, projected by ``QUERY`` below. Resource Graph returns ``type``
  lowercased; the table maps it to ARM's spelling and the canonical type. A
  type not in the table is kept as sent and typed ``other``, never dropped.

A row without ``id``, ``type`` or ``subscriptionId`` is rejected and counted;
it stays in bronze. Every projected column not mapped (resource group, kind,
SKU, zones, anything Resource Graph adds) is kept in ``raw_extra``. Tags are
customer-written: stored verbatim as data, never read as instruction
(CLAUDE.md rule 4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

SOURCE: Final = "azure"
ENTITY: Final = "resource"
COST_EXPORT: Final = "cost_export"  # FOCUS exports -> cost_fact (cost_source.py)
COST_TOTAL: Final = "cost_total"  # month-to-date totals -> billing_total (billing_source.py)
INVOICE: Final = "invoice"  # issued invoices -> billing_total (billing_source.py)
COST_DAILY: Final = "cost_daily"  # query API, daily -> cost_fact (daily_source.py)
# Cloud Services audit (ADR 0038) -> cloud_check (audit.py, audit_source.py)
BACKUP_ITEM: Final = "backup_item"
POLICY_STATE: Final = "policy_state"
ADVISOR: Final = "advisor"
DEFENDER: Final = "defender"
RESOURCE_CONFIG: Final = "resource_config"
AUDIT: Final = frozenset({BACKUP_ITEM, POLICY_STATE, ADVISOR, DEFENDER, RESOURCE_CONFIG})
ENTITIES: Final = frozenset({ENTITY, COST_EXPORT, COST_TOTAL, INVOICE, COST_DAILY})
# A customer credential's source may also run the audit (a binding is required).
ALL_ENTITIES: Final = ENTITIES | AUDIT
OTHER: Final = "other"

# Fixed: the connector asks for exactly these columns. ``id`` must be projected
# for Resource Graph to page with $skipToken.
QUERY: Final = (
    "Resources"
    " | project id, name, type, location, tags, subscriptionId, resourceGroup, tenantId,"
    " kind, sku,"
    " osName = tostring(properties.extended.instanceView.osName),"
    " osType = tostring(properties.storageProfile.osDisk.osType),"
    " powerState = tostring(properties.extended.instanceView.powerState.code),"
    " provisioningState = tostring(properties.provisioningState)"
    " | order by id asc"
)

# Resource Graph type (lowercase) -> (ARM spelling, canonical type). The first
# four are what the synthetic estate holds (synthetic's _AZURE_TYPES); the rest
# are the types a real subscription holds most. Canonical names are shared with
# AWS (aws/mapping.py) where the thing is the same, and with the rate card's
# catalogue (cloudops_commercial.ratecard) where it has a row: a type mapped to
# one of those is priced from then on.
TYPES: Final[Mapping[str, tuple[str, str]]] = {
    t.lower(): (t, canonical) for t, canonical in (
        ("Microsoft.Compute/virtualMachines", "compute.vm"),
        ("Microsoft.Sql/servers/databases", "database.sql"),
        ("Microsoft.Storage/storageAccounts", "storage.account"),
        ("Microsoft.Web/sites", "web.app"),
        ("Microsoft.Network/loadBalancers", "network.load_balancer"),
        # Compute
        ("Microsoft.Compute/virtualMachines/extensions", "compute.vm_extension"),
        ("Microsoft.Compute/virtualMachineScaleSets", "compute.vm_scale_set"),
        ("Microsoft.Compute/availabilitySets", "compute.availability_set"),
        ("Microsoft.Compute/images", "compute.image"),
        ("Microsoft.Compute/galleries", "compute.image_gallery"),
        ("Microsoft.Compute/galleries/images", "compute.image"),
        ("Microsoft.Compute/galleries/images/versions", "compute.image_version"),
        ("Microsoft.Compute/sshPublicKeys", "security.ssh_key"),
        ("Microsoft.ContainerService/managedClusters", "compute.kubernetes"),
        ("Microsoft.ContainerRegistry/registries", "container.registry"),
        ("Microsoft.App/containerApps", "compute.container_app"),
        ("Microsoft.App/managedEnvironments", "compute.container_app_environment"),
        ("Microsoft.Web/serverFarms", "web.app_plan"),
        # Storage
        ("Microsoft.Compute/disks", "storage.disk"),
        ("Microsoft.Compute/snapshots", "storage.snapshot"),
        ("Microsoft.RecoveryServices/vaults", "backup.vault"),
        # Network
        ("Microsoft.Network/networkInterfaces", "network.interface"),
        ("Microsoft.Network/publicIPAddresses", "network.public_ip"),
        ("Microsoft.Network/virtualNetworks", "network.vnet"),
        ("Microsoft.Network/virtualNetworks/subnets", "network.subnet"),
        ("Microsoft.Network/networkSecurityGroups", "network.security_group"),
        ("Microsoft.Network/routeTables", "network.route_table"),
        ("Microsoft.Network/natGateways", "network.nat_gateway"),
        ("Microsoft.Network/privateEndpoints", "network.private_endpoint"),
        ("Microsoft.Network/privateDnsZones", "network.dns_zone"),
        ("Microsoft.Network/privateDnsZones/virtualNetworkLinks", "network.dns_zone_link"),
        ("Microsoft.Network/dnsZones", "network.dns_zone"),
        ("Microsoft.Network/networkWatchers", "network.watcher"),
        ("Microsoft.Network/bastionHosts", "network.bastion"),
        ("Microsoft.Network/applicationGateways", "network.application_gateway"),
        ("Microsoft.Network/azureFirewalls", "network.firewall"),
        ("Microsoft.Network/virtualNetworkGateways", "network.vpn_gateway"),
        # Security and identity
        ("Microsoft.KeyVault/vaults", "security.key_vault"),
        ("Microsoft.ManagedIdentity/userAssignedIdentities", "identity.managed_identity"),
        # Databases and messaging
        ("Microsoft.Sql/servers", "database.sql_server"),
        ("Microsoft.DBforPostgreSQL/flexibleServers", "database.postgresql"),
        ("Microsoft.DBforMySQL/flexibleServers", "database.mysql"),
        ("Microsoft.DocumentDB/databaseAccounts", "database.nosql"),
        ("Microsoft.Cache/Redis", "database.cache"),
        ("Microsoft.EventHub/namespaces", "messaging.event_stream"),
        ("Microsoft.ServiceBus/namespaces", "messaging.queue"),
        # Monitoring
        ("Microsoft.OperationalInsights/workspaces", "monitoring.log_workspace"),
        ("Microsoft.Insights/components", "monitoring.app_insights"),
        ("Microsoft.Insights/actionGroups", "monitoring.action_group"),
        ("Microsoft.Insights/dataCollectionRules", "monitoring.data_collection_rule"),
        ("Microsoft.OperationsManagement/solutions", "monitoring.solution"),
    )
}

MAPPED: Final = frozenset({
    "id", "name", "type", "location", "tags", "subscriptionId", "osName", "osType",
    "powerState", "provisioningState",
})
# Projected by QUERY, so on every row: a page without one has drifted.
EXPECTED: Final = frozenset({"id", "name", "type", "location", "subscriptionId"})


class MalformedRecordError(ValueError):
    """A row without the fields that key it. Kept in bronze, counted, skipped."""


@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    display_name: str | None
    state: str | None


@dataclass(frozen=True)
class Resource:
    subscription_id: str
    native_id: str
    name: str
    type_native: str
    type_canonical: str
    region: str | None
    tags: dict[str, str]
    operating_system: str | None
    state: str | None
    raw_extra: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def subscription_id(value: Any) -> str | None:
    """GUIDs compare case-insensitively; aliases hold them lowercased."""
    text = _text(value)
    return None if text is None else text.lower()


def _json(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedRecordError("payload is not JSON") from None
    if not isinstance(doc, dict):
        raise MalformedRecordError("payload is not a JSON object")
    return doc


def subscriptions(body: bytes) -> list[Subscription]:
    """The listing's subscriptions; entries without an id are skipped."""
    out = []
    for s in _json(body).get("value") or []:
        sid = subscription_id(s.get("subscriptionId")) if isinstance(s, Mapping) else None
        if sid is not None:
            out.append(Subscription(sid, _text(s.get("displayName")), _text(s.get("state"))))
    return out


def next_link(body: bytes) -> str | None:
    return _text(_json(body).get("nextLink"))


def rows(body: bytes) -> list[Any]:
    """A Resource Graph page's rows, as sent (not yet validated)."""
    data = _json(body).get("data")
    return data if isinstance(data, list) else []


def skip_token(body: bytes) -> str | None:
    return _text(_json(body).get("$skipToken"))


def truncated(body: bytes) -> bool:
    """Resource Graph cut the result short without a skip token: not a snapshot."""
    return str(_json(body).get("resultTruncated", "false")).lower() == "true"


def _state(row: Mapping[str, Any]) -> str | None:
    power = _text(row.get("powerState"))  # "PowerState/running"
    if power is not None:
        return power.rsplit("/", 1)[-1]
    return _text(row.get("provisioningState"))


def _tags(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in value.items()}


def normalise(row: Any) -> Resource:
    if not isinstance(row, Mapping):
        raise MalformedRecordError("row is not an object")
    native_id, kind = _text(row.get("id")), _text(row.get("type"))
    sub = subscription_id(row.get("subscriptionId"))
    if native_id is None or kind is None or sub is None:
        raise MalformedRecordError("id, type and subscriptionId are required")
    type_native, canonical = TYPES.get(kind.lower(), (kind, OTHER))
    return Resource(
        subscription_id=sub,
        native_id=native_id,
        name=_text(row.get("name")) or native_id.rstrip("/").rsplit("/", 1)[-1],
        type_native=type_native,
        type_canonical=canonical,
        region=_text(row.get("location")),
        tags=_tags(row.get("tags")),
        operating_system=_text(row.get("osName")) or _text(row.get("osType")),
        state=_state(row),
        raw_extra={k: v for k, v in row.items() if k not in MAPPED},
    )
