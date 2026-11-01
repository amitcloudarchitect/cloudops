"""Cloud Services audit (ADR 0038): Azure payloads -> ``cloudops.cloud_check`` rows.

Five entities, each a snapshot of one kind of check on a customer's subscriptions,
read with the customer's own Reader credential (ADR 0027):

=====================  ==========================================  ==============
entity                 read from                                   kind
=====================  ==========================================  ==============
``backup_item``        Resource Graph ``recoveryservicesresources``  ``backup``
``policy_state``       Resource Graph ``policyresources``            ``policy``
``advisor``            Resource Graph ``advisorresources``           ``advisor``
``defender``           ARM ``Microsoft.Security/assessments``        ``defender``
``resource_config``    Resource Graph ``Resources``                  ``config``
=====================  ==========================================  ==============

Defender is read from ARM, not Resource Graph: in dev, ``securityresources``
answered no rows for a subscription whose assessments ARM listed (596), under the
same Reader credential.

Every query is fixed: the connector asks for exactly these columns, and ``id`` is
projected so Resource Graph can page with ``$skipToken``. Each row becomes one
``Check``: the thing checked (a resource id, lowercased), its state (``ok``,
``issue`` or ``not_applicable``), a severity (``high``, ``medium``, ``low``,
``info``) and a ``detail`` of fixed keys. Free text the provider sends (a
recommendation's problem and solution, a policy's names, a resource name) is
customer- or attacker-influenceable: it is stored verbatim as data and never read
as instruction (CLAUDE.md rule 4). Nothing here is sent to a model.

**Configuration rules are the platform's policy** (``RULES``), modelled like
ADR 0036's targets: each reads one projected setting and says ``issue`` or ``ok``
at a fixed severity. A resource also gets one ``settings`` check (state ``ok``,
severity ``info``) holding every projected setting, so a change to any of them is
recorded in its history.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.mapping import MalformedRecordError, subscription_id

BACKUP: Final = mapping.BACKUP_ITEM
POLICY: Final = mapping.POLICY_STATE
ADVISOR: Final = mapping.ADVISOR
DEFENDER: Final = mapping.DEFENDER
CONFIG: Final = mapping.RESOURCE_CONFIG
ENTITIES: Final = mapping.AUDIT
GRAPH_ENTITIES: Final = frozenset({BACKUP, POLICY, ADVISOR, CONFIG})
KIND: Final[Mapping[str, str]] = {BACKUP: "backup", POLICY: "policy", ADVISOR: "advisor",
                                  DEFENDER: "defender", CONFIG: "config"}

State = Literal["ok", "issue", "not_applicable"]
Severity = Literal["high", "medium", "low", "info"]
SEVERITIES: Final[tuple[Severity, ...]] = ("high", "medium", "low", "info")

# Azure Backup's "never" for a last backup time.
NEVER: Final = frozenset({"2001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"})

QUERIES: Final[Mapping[str, str]] = {
    BACKUP: (
        "recoveryservicesresources"
        " | where type =~ 'microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers"
        "/protecteditems'"
        " | extend policyKey = tolower(tostring(properties.policyId))"
        " | join kind=leftouter (recoveryservicesresources"
        "   | where type =~ 'microsoft.recoveryservices/vaults/backuppolicies'"
        "   | project policyKey = tolower(id),"
        "     retentionDays = toint(properties.retentionPolicy.dailySchedule"
        ".retentionDuration['count']),"
        "     retentionWeeks = toint(properties.retentionPolicy.weeklySchedule"
        ".retentionDuration['count']),"
        "     scheduleFrequency = tostring(properties.schedulePolicy.scheduleRunFrequency))"
        "   on policyKey"
        " | project id, subscriptionId, location,"
        " vaultId = tostring(properties.vaultId),"
        # The protected resource as inventory holds it (a file share's storage
        # account); the data source itself (the share) beside it.
        " sourceResourceId = iff(isnotempty(properties.sourceResourceId),"
        "   tostring(properties.sourceResourceId),"
        "   tostring(properties.dataSourceInfo.resourceID)),"
        " dataSourceId = tostring(properties.dataSourceInfo.resourceID),"
        " dataSourceType = tostring(properties.dataSourceInfo.datasourceType),"
        " workloadType = tostring(properties.workloadType),"
        " protectionState = tostring(properties.protectionState),"
        " lastBackupStatus = tostring(properties.lastBackupStatus),"
        " lastBackupTime = tostring(properties.lastBackupTime),"
        " lastRecoveryPoint = tostring(properties.lastRecoveryPoint),"
        " healthStatus = tostring(properties.healthStatus),"
        " policyName = tostring(properties.policyName),"
        " retentionDays, retentionWeeks, scheduleFrequency"
        " | order by id asc"
    ),
    POLICY: (
        "policyresources"
        " | where type =~ 'microsoft.policyinsights/policystates'"
        " | extend assignmentKey = tolower(tostring(properties.policyAssignmentId))"
        " | join kind=leftouter (policyresources"
        "   | where type =~ 'microsoft.authorization/policyassignments'"
        "   | project assignmentKey = tolower(id),"
        "     assignmentDisplayName = tostring(properties.displayName))"
        "   on assignmentKey"
        " | project id, subscriptionId,"
        " resourceId = tostring(properties.resourceId),"
        " resourceType = tostring(properties.resourceType),"
        " resourceGroup = tostring(properties.resourceGroup),"
        " resourceLocation = tostring(properties.resourceLocation),"
        " complianceState = tostring(properties.complianceState),"
        " policyAssignmentId = tostring(properties.policyAssignmentId),"
        " policyAssignmentName = tostring(properties.policyAssignmentName),"
        " assignmentDisplayName,"
        " policyDefinitionId = tostring(properties.policyDefinitionId),"
        " policyDefinitionName = tostring(properties.policyDefinitionName),"
        " policyDefinitionReferenceId = tostring(properties.policyDefinitionReferenceId),"
        " policyDefinitionAction = tostring(properties.policyDefinitionAction),"
        " policySetDefinitionCategory = tostring(properties.policySetDefinitionCategory),"
        " timestamp = tostring(properties.timestamp)"
        " | order by id asc"
    ),
    ADVISOR: (
        "advisorresources"
        " | where type =~ 'microsoft.advisor/recommendations'"
        " | project id, subscriptionId,"
        " resourceId = tostring(properties.resourceMetadata.resourceId),"
        " category = tostring(properties.category),"
        " impact = tostring(properties.impact),"
        " impactedField = tostring(properties.impactedField),"
        " impactedValue = tostring(properties.impactedValue),"
        " problem = tostring(properties.shortDescription.problem),"
        " solution = tostring(properties.shortDescription.solution),"
        " recommendationTypeId = tostring(properties.recommendationTypeId),"
        " lastUpdated = tostring(properties.lastUpdated),"
        " savingsAmount = tostring(properties.extendedProperties.savingsAmount),"
        " annualSavingsAmount = tostring(properties.extendedProperties.annualSavingsAmount),"
        " savingsCurrency = tostring(properties.extendedProperties.savingsCurrency)"
        " | order by id asc"
    ),
    CONFIG: (
        "Resources"
        " | where type in~ ('microsoft.compute/virtualmachines',"
        " 'microsoft.storage/storageaccounts', 'microsoft.sql/servers',"
        " 'microsoft.sql/servers/databases', 'microsoft.dbforpostgresql/flexibleservers',"
        " 'microsoft.dbformysql/flexibleservers', 'microsoft.keyvault/vaults',"
        " 'microsoft.containerservice/managedclusters', 'microsoft.web/sites',"
        " 'microsoft.compute/disks', 'microsoft.network/publicipaddresses')"
        " | extend t = tolower(type), key = tolower(id)"
        " | join kind=leftouter (Resources"
        "   | where type =~ 'microsoft.network/networkinterfaces'"
        "   | mv-expand ipc = properties.ipConfigurations"
        "   | where isnotempty(ipc.properties.publicIPAddress.id)"
        "   | summarize publicIps = count()"
        "     by key = tolower(tostring(properties.virtualMachine.id)))"
        "   on key"
        " | extend settings = case("
        "  t == 'microsoft.compute/virtualmachines', pack("
        "   'vmSize', properties.hardwareProfile.vmSize,"
        "   'osType', properties.storageProfile.osDisk.osType,"
        "   'osName', properties.extended.instanceView.osName,"
        "   'osVersion', properties.extended.instanceView.osVersion,"
        "   'powerState', properties.extended.instanceView.powerState.code,"
        "   'encryptionAtHost', properties.securityProfile.encryptionAtHost,"
        "   'securityType', properties.securityProfile.securityType,"
        "   'diskEncryptionSet', isnotempty(properties.storageProfile.osDisk.managedDisk"
        ".diskEncryptionSet.id),"
        "   'bootDiagnostics', properties.diagnosticsProfile.bootDiagnostics.enabled,"
        "   'priority', properties.priority, 'zones', zones,"
        "   'publicIps', coalesce(publicIps, 0)),"
        "  t == 'microsoft.storage/storageaccounts', pack("
        "   'sku', sku.name, 'kind', kind,"
        "   'allowBlobPublicAccess', properties.allowBlobPublicAccess,"
        "   'minimumTlsVersion', properties.minimumTlsVersion,"
        "   'supportsHttpsTrafficOnly', properties.supportsHttpsTrafficOnly,"
        "   'publicNetworkAccess', properties.publicNetworkAccess,"
        "   'networkDefaultAction', properties.networkAcls.defaultAction,"
        "   'allowSharedKeyAccess', properties.allowSharedKeyAccess,"
        "   'infrastructureEncryption', properties.encryption.requireInfrastructureEncryption,"
        "   'keySource', properties.encryption.keySource),"
        "  t == 'microsoft.sql/servers', pack("
        "   'version', properties.version,"
        "   'minimalTlsVersion', properties.minimalTlsVersion,"
        "   'publicNetworkAccess', properties.publicNetworkAccess,"
        "   'entraOnlyAuth', properties.administrators.azureADOnlyAuthentication),"
        "  t == 'microsoft.sql/servers/databases', pack("
        "   'sku', sku.name, 'tier', sku.tier,"
        "   'zoneRedundant', properties.zoneRedundant,"
        "   'backupStorageRedundancy', properties.currentBackupStorageRedundancy,"
        "   'status', properties.status),"
        "  t in ('microsoft.dbforpostgresql/flexibleservers',"
        "        'microsoft.dbformysql/flexibleservers'), pack("
        "   'version', properties.version, 'sku', sku.name, 'tier', sku.tier,"
        "   'backupRetentionDays', properties.backup.backupRetentionDays,"
        "   'geoRedundantBackup', properties.backup.geoRedundantBackup,"
        "   'highAvailability', properties.highAvailability.mode,"
        "   'publicNetworkAccess', properties.network.publicNetworkAccess,"
        "   'storageGb', properties.storage.storageSizeGB,"
        "   'state', properties.state),"
        "  t == 'microsoft.keyvault/vaults', pack("
        "   'sku', properties.sku.name,"
        "   'enableSoftDelete', properties.enableSoftDelete,"
        "   'softDeleteRetentionDays', properties.softDeleteRetentionInDays,"
        "   'enablePurgeProtection', properties.enablePurgeProtection,"
        "   'enableRbacAuthorization', properties.enableRbacAuthorization,"
        "   'publicNetworkAccess', properties.publicNetworkAccess,"
        "   'networkDefaultAction', properties.networkAcls.defaultAction),"
        "  t == 'microsoft.containerservice/managedclusters', pack("
        "   'kubernetesVersion', properties.kubernetesVersion,"
        "   'enableRbac', properties.enableRBAC,"
        "   'entraManaged', properties.aadProfile.managed,"
        "   'privateCluster', properties.apiServerAccessProfile.enablePrivateCluster,"
        "   'networkPlugin', properties.networkProfile.networkPlugin,"
        "   'tier', sku.tier),"
        "  t == 'microsoft.web/sites', pack("
        "   'kind', kind, 'httpsOnly', properties.httpsOnly,"
        "   'clientCertEnabled', properties.clientCertEnabled,"
        "   'publicNetworkAccess', properties.publicNetworkAccess,"
        "   'state', properties.state),"
        "  t == 'microsoft.compute/disks', pack("
        "   'sku', sku.name, 'diskSizeGb', properties.diskSizeGB,"
        "   'diskState', properties.diskState,"
        "   'encryptionType', properties.encryption.type,"
        "   'networkAccessPolicy', properties.networkAccessPolicy,"
        "   'publicNetworkAccess', properties.publicNetworkAccess),"
        "  t == 'microsoft.network/publicipaddresses', pack("
        "   'sku', sku.name, 'allocation', properties.publicIPAllocationMethod,"
        "   'attached', isnotempty(properties.ipConfiguration.id)"
        "               or isnotempty(properties.natGateway.id)),"
        "  dynamic(null))"
        " | project id, name, type, location, subscriptionId, resourceGroup, settings"
        " | order by id asc"
    ),
}

# Projected by each query, so on every row: a page without one has drifted.
EXPECTED: Final[Mapping[str, frozenset[str]]] = {
    BACKUP: frozenset({"id", "subscriptionId", "sourceResourceId", "protectionState"}),
    POLICY: frozenset({"id", "subscriptionId", "resourceId", "complianceState",
                       "policyAssignmentId"}),
    ADVISOR: frozenset({"id", "subscriptionId", "category", "impact"}),
    CONFIG: frozenset({"id", "type", "subscriptionId", "settings"}),
    DEFENDER: frozenset({"id", "name", "properties"}),
}
MAPPED: Final[Mapping[str, frozenset[str]]] = {
    BACKUP: EXPECTED[BACKUP] | {"location", "vaultId", "dataSourceId", "dataSourceType",
                                "workloadType", "lastBackupStatus", "lastBackupTime",
                                "lastRecoveryPoint", "healthStatus", "policyName", "retentionDays",
                                "retentionWeeks", "scheduleFrequency"},
    POLICY: EXPECTED[POLICY] | {"resourceType", "resourceGroup", "resourceLocation",
                                "policyAssignmentName", "assignmentDisplayName",
                                "policyDefinitionId", "policyDefinitionName",
                                "policyDefinitionReferenceId", "policyDefinitionAction",
                                "policySetDefinitionCategory", "timestamp"},
    ADVISOR: EXPECTED[ADVISOR] | {"resourceId", "impactedField", "impactedValue", "problem",
                                  "solution", "recommendationTypeId", "lastUpdated",
                                  "savingsAmount", "annualSavingsAmount", "savingsCurrency"},
    CONFIG: EXPECTED[CONFIG] | {"name", "location", "resourceGroup"},
    DEFENDER: EXPECTED[DEFENDER] | {"type"},
}

MAX_TEXT: Final = 2000  # a provider's free text, kept as data, bounded


@dataclass(frozen=True)
class Check:
    subscription_id: str
    native_id: str  # unique per account and kind
    resource_native_id: str | None  # lowercased ARM id of what was checked
    resource_type: str | None  # lowercased
    resource_group: str | None
    region: str | None
    category: str
    rule: str
    title: str
    state: State
    severity: Severity
    source_status: str | None
    observed_at: str | None  # ISO, the provider's own time where it gives one
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """What a change in history is: state, severity or any detail value."""
        doc = json.dumps([self.state, self.severity, self.source_status, self.title,
                          self.detail], sort_keys=True, default=str)
        return hashlib.sha256(doc.encode()).hexdigest()


def _text(value: Any, limit: int = 400) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _lower(value: Any) -> str | None:
    text = _text(value, MAX_TEXT)
    return None if text is None else text.lower()


def _group(resource_id: str | None) -> str | None:
    """``…/resourceGroups/<name>/…`` -> ``<name>`` (lowercased with the id)."""
    if resource_id is None:
        return None
    parts = resource_id.split("/")
    for i, part in enumerate(parts[:-1]):
        if part == "resourcegroups":
            return parts[i + 1] or None
    return None


def _type_of(resource_id: str | None) -> str | None:
    """The ARM type an id names: providers/<ns>/<type>/<name>[/<child>/<name>…]."""
    if resource_id is None or "/providers/" not in resource_id:
        return None
    tail = resource_id.rsplit("/providers/", 1)[1].split("/")
    if len(tail) < 3:
        return None
    return "/".join([tail[0], *tail[1::2]])


def _row(row: Any) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise MalformedRecordError("row is not an object")
    return row


def _keyed(row: Mapping[str, Any]) -> tuple[str, str]:
    native, sub = _lower(row.get("id")), subscription_id(row.get("subscriptionId"))
    if native is None or sub is None:
        raise MalformedRecordError("id and subscriptionId are required")
    return native, sub


# --- backup -------------------------------------------------------------------------

BACKUP_FAILED: Final = frozenset({"failed", "completedwithwarnings"})


def backup(row: Any) -> list[Check]:
    r = _row(row)
    native, sub = _keyed(r)
    source = _lower(r.get("sourceResourceId"))
    if source is None:
        raise MalformedRecordError("a protected item names its data source")
    protection = _text(r.get("protectionState"))
    status = _text(r.get("lastBackupStatus"))
    health = _text(r.get("healthStatus"))
    last = _text(r.get("lastBackupTime"))
    last = None if last in NEVER else last
    protecting = (protection or "").lower() in {"protected", "irpending"}
    failed = (status or "").lower() in BACKUP_FAILED or (health or "").lower() in {
        "actionrequired", "unhealthy"}
    state: State
    severity: Severity
    if not protecting or failed:
        state, severity = "issue", "high"
    elif last is None:
        state, severity = "issue", "medium"  # protection configured, no backup yet
    else:
        state, severity = "ok", "info"
    return [Check(
        sub, native, source, _lower(r.get("dataSourceType")) or _type_of(source),
        _group(source), _text(r.get("location")), "backup", "backup.protected_item",
        "Azure Backup protected item", state, severity,
        "/".join(x for x in (protection, status, health) if x) or None, last,
        {"vault": _text(_lower(r.get("vaultId")), MAX_TEXT),
         "data_source": _lower(r.get("dataSourceId")),
         "workload": _text(r.get("workloadType")),
         "protection_state": protection, "last_backup_status": status,
         "health": health, "last_backup_at": last,
         "last_recovery_point_at": _text(r.get("lastRecoveryPoint")),
         "policy": _text(r.get("policyName")),
         "retention_days": _int(r.get("retentionDays")),
         "retention_weeks": _int(r.get("retentionWeeks")),
         "frequency": _text(r.get("scheduleFrequency"))},
    )]


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- policy -------------------------------------------------------------------------

POLICY_STATE: Final[Mapping[str, State]] = {
    "noncompliant": "issue", "compliant": "ok", "exempt": "not_applicable",
    "conflict": "issue", "notstarted": "not_applicable", "unknown": "not_applicable",
}


def policy(row: Any) -> list[Check]:
    r = _row(row)
    native, sub = _keyed(r)
    resource = _lower(r.get("resourceId"))
    assignment = _lower(r.get("policyAssignmentId"))
    compliance = _text(r.get("complianceState"))
    if resource is None or assignment is None or compliance is None:
        raise MalformedRecordError("a policy state names its resource, assignment and state")
    state = POLICY_STATE.get(compliance.lower(), "not_applicable")
    effect = (_text(r.get("policyDefinitionAction")) or "").lower()
    # A deny or deploy policy failing matters more than an audit one.
    severity: Severity = ("info" if state != "issue" else
                          "medium" if effect in {"deny", "deployifnotexists", "modify"} else "low")
    title = (_text(r.get("assignmentDisplayName")) or _text(r.get("policyAssignmentName"))
             or "Policy assignment")
    return [Check(
        sub, native, resource, _lower(r.get("resourceType")) or _type_of(resource),
        _lower(r.get("resourceGroup")) or _group(resource), _text(r.get("resourceLocation")),
        _text(r.get("policySetDefinitionCategory")) or "policy", "policy.state",
        title, state, severity, compliance, _text(r.get("timestamp")),
        {"assignment": assignment, "assignment_name": _text(r.get("policyAssignmentName")),
         "definition": _lower(r.get("policyDefinitionId")),
         "definition_name": _text(r.get("policyDefinitionName")),
         "reference": _text(r.get("policyDefinitionReferenceId")),
         "effect": _text(r.get("policyDefinitionAction"))},
    )]


# --- Advisor ------------------------------------------------------------------------

IMPACT: Final[Mapping[str, Severity]] = {"high": "high", "medium": "medium", "low": "low"}
# Advisor's names for its five pillars -> the platform's.
PILLAR: Final[Mapping[str, str]] = {
    "cost": "cost", "security": "security", "highavailability": "reliability",
    "performance": "performance", "operationalexcellence": "operational_excellence",
}


def advisor(row: Any) -> list[Check]:
    r = _row(row)
    native, sub = _keyed(r)
    category = _text(r.get("category"))
    if category is None:
        raise MalformedRecordError("a recommendation has a category")
    resource = _lower(r.get("resourceId"))
    return [Check(
        sub, native, resource, _type_of(resource), _group(resource), None,
        PILLAR.get(category.lower(), category.lower()), "advisor.recommendation",
        _text(r.get("problem"), MAX_TEXT) or "Advisor recommendation", "issue",
        IMPACT.get((_text(r.get("impact")) or "").lower(), "low"), _text(r.get("impact")),
        _text(r.get("lastUpdated")),
        {"solution": _text(r.get("solution"), MAX_TEXT),
         "impacted_field": _text(r.get("impactedField")),
         "impacted_value": _text(r.get("impactedValue")),
         "recommendation_type": _lower(r.get("recommendationTypeId")),
         "savings": _text(r.get("savingsAmount")),
         "annual_savings": _text(r.get("annualSavingsAmount")),
         "savings_currency": _text(r.get("savingsCurrency"))},
    )]


# --- Defender for Cloud -------------------------------------------------------------

DEFENDER_STATE: Final[Mapping[str, State]] = {
    "unhealthy": "issue", "healthy": "ok", "notapplicable": "not_applicable"}


def defender_page(body: bytes) -> list[Any]:
    doc = json.loads(body)
    value = doc.get("value") if isinstance(doc, dict) else None
    return value if isinstance(value, list) else []


def defender_next(body: bytes) -> str | None:
    doc = json.loads(body)
    return _text(doc.get("nextLink"), MAX_TEXT) if isinstance(doc, dict) else None


def defender(row: Any, subscription: str) -> list[Check]:
    """One assessment. ARM's listing carries no ``subscriptionId``: the request's."""
    r = _row(row)
    native = _lower(r.get("id"))
    props = r.get("properties")
    if native is None or not isinstance(props, Mapping):
        raise MalformedRecordError("an assessment has an id and properties")
    found = props.get("status")
    status: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    code = _text(status.get("code"))
    if code is None:
        raise MalformedRecordError("an assessment has a status")
    details = props.get("resourceDetails")
    details = details if isinstance(details, Mapping) else {}
    resource = _lower(details.get("Id") or details.get("ResourceId") or details.get("id"))
    if resource is None and "/providers/microsoft.security/assessments/" in native:
        resource = native.split("/providers/microsoft.security/assessments/")[0]
    meta = props.get("metadata")
    meta = meta if isinstance(meta, Mapping) else {}
    state = DEFENDER_STATE.get(code.lower(), "not_applicable")
    sev = IMPACT.get((_text(meta.get("severity")) or "").lower(), "medium")
    categories = meta.get("categories")
    return [Check(
        subscription, native, resource, _type_of(resource), _group(resource), None,
        "security", "defender.assessment",
        _text(props.get("displayName"), MAX_TEXT) or "Defender for Cloud assessment",
        state, sev if state == "issue" else "info", code, None,
        {"cause": _text(status.get("cause")), "status_description":
            _text(status.get("description"), MAX_TEXT),
         "assessment": _lower(r.get("name")),
         "categories": [str(c) for c in categories][:10] if isinstance(categories, list) else [],
         "threats": [str(c) for c in meta.get("threats") or []][:10]
         if isinstance(meta.get("threats"), list) else [],
         "severity": _text(meta.get("severity")),
         "source": _text(details.get("Source"))},
    )]


# --- resource configuration ---------------------------------------------------------

Test = Callable[[Mapping[str, Any]], bool | None]  # True: issue; None: not readable


@dataclass(frozen=True)
class Rule:
    rule: str
    resource_type: str  # lowercased ARM type
    title: str
    severity: Severity
    category: str
    keys: tuple[str, ...]  # the settings it reads, kept in its detail
    issue: Test


def _flag(settings: Mapping[str, Any], key: str) -> bool | None:
    v = settings.get(key)
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in {"true", "false"}:
        return v.lower() == "true"
    return None


def _is(settings: Mapping[str, Any], key: str, *values: str) -> bool | None:
    v = settings.get(key)
    if not isinstance(v, str) or not v:
        return None
    return v.lower() in {x.lower() for x in values}


def _not(v: bool | None) -> bool | None:
    return None if v is None else not v


def _tls_below_12(settings: Mapping[str, Any], key: str) -> bool | None:
    v = settings.get(key)
    if not isinstance(v, str) or not v:
        return None
    digits = v.upper().replace("TLS", "").replace("_", ".").strip(". ")
    return digits in {"1.0", "1", "1.1"}


def _below(settings: Mapping[str, Any], key: str, floor: int) -> bool | None:
    v = _int(settings.get(key))
    return None if v is None else v < floor


VM: Final = "microsoft.compute/virtualmachines"
STORAGE: Final = "microsoft.storage/storageaccounts"
SQL_SERVER: Final = "microsoft.sql/servers"
SQL_DB: Final = "microsoft.sql/servers/databases"
POSTGRES: Final = "microsoft.dbforpostgresql/flexibleservers"
MYSQL: Final = "microsoft.dbformysql/flexibleservers"
KEY_VAULT: Final = "microsoft.keyvault/vaults"
AKS: Final = "microsoft.containerservice/managedclusters"
WEB: Final = "microsoft.web/sites"
DISK: Final = "microsoft.compute/disks"
PUBLIC_IP: Final = "microsoft.network/publicipaddresses"
# The types the backup view expects an Azure Backup protected item for (ADR 0038).
# PaaS databases back themselves up; their retention and redundancy are rules.
BACKUP_EXPECTED: Final = frozenset({VM})

def _db(t: str) -> str:
    """``microsoft.dbforpostgresql/flexibleservers`` -> ``dbforpostgresql``."""
    return t.split(".", 1)[1].split("/", 1)[0]


RULES: Final[tuple[Rule, ...]] = (
    Rule("vm.public_ip", VM, "Virtual machine has a public IP address", "high", "security",
         ("publicIps",), lambda s: None if _int(s.get("publicIps")) is None
         else _int(s.get("publicIps")) != 0),
    Rule("vm.encryption_at_host", VM, "Encryption at host is not enabled", "low", "security",
         ("encryptionAtHost",), lambda s: _flag(s, "encryptionAtHost") is not True),
    Rule("vm.boot_diagnostics", VM, "Boot diagnostics are off", "low",
         "operational_excellence", ("bootDiagnostics",),
         lambda s: _not(_flag(s, "bootDiagnostics"))),
    Rule("storage.public_blob_access", STORAGE, "Storage account allows public blob access",
         "high", "security", ("allowBlobPublicAccess",),
         lambda s: _flag(s, "allowBlobPublicAccess")),
    Rule("storage.https_only", STORAGE, "Storage account accepts HTTP", "high", "security",
         ("supportsHttpsTrafficOnly",), lambda s: _not(_flag(s, "supportsHttpsTrafficOnly"))),
    Rule("storage.min_tls", STORAGE, "Storage account accepts TLS below 1.2", "medium",
         "security", ("minimumTlsVersion",), lambda s: _tls_below_12(s, "minimumTlsVersion")),
    Rule("storage.public_network", STORAGE, "Storage account is open to all networks",
         "medium", "security", ("networkDefaultAction", "publicNetworkAccess"),
         lambda s: None if s.get("networkDefaultAction") is None
         else bool(_is(s, "networkDefaultAction", "Allow"))
         and not _is(s, "publicNetworkAccess", "Disabled")),
    # Unset means allowed (Azure's default).
    Rule("storage.shared_key", STORAGE, "Storage account allows shared key access", "low",
         "security", ("allowSharedKeyAccess",),
         lambda s: _flag(s, "allowSharedKeyAccess") is not False),
    # Unset means no minimum.
    Rule("sql.min_tls", SQL_SERVER, "SQL server accepts TLS below 1.2", "medium", "security",
         ("minimalTlsVersion",), lambda s: True if s.get("minimalTlsVersion") in (None, "",
                                                                                "None")
         else _tls_below_12(s, "minimalTlsVersion")),
    Rule("sql.public_network", SQL_SERVER, "SQL server allows public network access",
         "medium", "security", ("publicNetworkAccess",),
         lambda s: _is(s, "publicNetworkAccess", "Enabled")),
    Rule("sql_db.local_backup_storage", SQL_DB,
         "Database backups are not geo-redundant", "low", "reliability",
         ("backupStorageRedundancy",), lambda s: _is(s, "backupStorageRedundancy", "Local",
                                                     "Zone")),
    *(r for t in (POSTGRES, MYSQL) for r in (
        Rule(f"{_db(t)}.public_network", t, "Database server allows public network access",
             "medium", "security", ("publicNetworkAccess",),
             lambda s: _is(s, "publicNetworkAccess", "Enabled")),
        Rule(f"{_db(t)}.backup_retention", t, "Backup retention is under 7 days", "medium",
             "reliability", ("backupRetentionDays",),
             lambda s: _below(s, "backupRetentionDays", 7)),
        Rule(f"{_db(t)}.geo_redundant_backup", t, "Backups are not geo-redundant", "low",
             "reliability", ("geoRedundantBackup",),
             lambda s: _is(s, "geoRedundantBackup", "Disabled")),
    )),
    # Unset means off.
    Rule("keyvault.purge_protection", KEY_VAULT, "Key vault purge protection is off",
         "medium", "reliability", ("enablePurgeProtection",),
         lambda s: _flag(s, "enablePurgeProtection") is not True),
    Rule("keyvault.soft_delete", KEY_VAULT, "Key vault soft delete is off", "high",
         "reliability", ("enableSoftDelete",), lambda s: _not(_flag(s, "enableSoftDelete"))),
    Rule("keyvault.public_network", KEY_VAULT, "Key vault is open to all networks", "medium",
         "security", ("publicNetworkAccess", "networkDefaultAction"),
         lambda s: None if s.get("publicNetworkAccess") is None else
         bool(_is(s, "publicNetworkAccess", "Enabled"))
         and not _is(s, "networkDefaultAction", "Deny")),
    Rule("aks.rbac", AKS, "Kubernetes RBAC is off", "high", "security", ("enableRbac",),
         lambda s: _not(_flag(s, "enableRbac"))),
    # Unset means a public API server.
    Rule("aks.public_api", AKS, "Kubernetes API server is public", "medium", "security",
         ("privateCluster",), lambda s: _flag(s, "privateCluster") is not True),
    Rule("web.https_only", WEB, "Web app accepts HTTP", "high", "security", ("httpsOnly",),
         lambda s: _not(_flag(s, "httpsOnly"))),
    Rule("disk.unattached", DISK, "Managed disk is not attached", "low", "cost",
         ("diskState",), lambda s: _is(s, "diskState", "Unattached")),
    Rule("disk.public_network", DISK, "Disk export is open to all networks", "low", "security",
         ("networkAccessPolicy",), lambda s: _is(s, "networkAccessPolicy", "AllowAll")),
    Rule("public_ip.unattached", PUBLIC_IP, "Public IP address is not attached", "low", "cost",
         ("attached",), lambda s: _not(_flag(s, "attached"))),
)
RULES_BY_TYPE: Final[Mapping[str, tuple[Rule, ...]]] = {
    t: tuple(r for r in RULES if r.resource_type == t) for t in {r.resource_type for r in RULES}
}
SETTINGS_RULE: Final = "config.settings"
_SETTING_TEXT: Final = 120


def _settings(value: Any) -> dict[str, Any]:
    """Scalars only, bounded: a setting is a flag, a number or a short name."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, bool | int | float) or v is None:
            out[str(k)] = v
        elif isinstance(v, str):
            out[str(k)] = v[:_SETTING_TEXT]
        elif isinstance(v, list):
            out[str(k)] = [str(x)[:_SETTING_TEXT] for x in v[:10]]
    return out


def config(row: Any) -> list[Check]:
    r = _row(row)
    native, sub = _keyed(r)
    kind = _lower(r.get("type"))
    if kind is None:
        raise MalformedRecordError("a resource has a type")
    settings = _settings(r.get("settings"))
    group = _lower(r.get("resourceGroup")) or _group(native)
    region = _text(r.get("location"))
    out = [Check(sub, f"{native}#{SETTINGS_RULE}", native, kind, group, region, "configuration",
                 SETTINGS_RULE, "Configuration", "ok", "info", None, None,
                 {"settings": settings})]
    for rule in RULES_BY_TYPE.get(kind, ()):
        try:
            issue = rule.issue(settings)
        except (TypeError, ValueError):
            issue = None
        state: State = "not_applicable" if issue is None else "issue" if issue else "ok"
        out.append(Check(
            sub, f"{native}#{rule.rule}", native, kind, group, region, rule.category,
            rule.rule, rule.title, state, rule.severity if state == "issue" else "info",
            None, None, {"settings": {k: settings.get(k) for k in rule.keys}}))
    return out


NORMALISE: Final[Mapping[str, Callable[[Any], list[Check]]]] = {
    BACKUP: backup, POLICY: policy, ADVISOR: advisor, CONFIG: config,
}
