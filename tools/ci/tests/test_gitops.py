"""The GitOps tree (step 12; LLD, Compute pod controls). Built with Kustomize and
checked object by object, so each rule fails the build if a manifest drops it.

Needs ``kubectl`` (for ``kubectl kustomize``); it errors, never skips, without it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GITOPS = REPO_ROOT / "infra/gitops"
WORKLOAD_KINDS = {"Deployment", "Rollout", "Job"}
OUR_NAMESPACES = {"platform-api", "platform-ingest", "platform-ai", "platform-web",
                  "platform-edge", "temporal", "monitoring"}
POOLS = {"app", "ingest", "telemetry", "ai"}
# Workloads that reach nothing in Azure, so hold no workload identity at all: the
# operations console (ADR 0017), whose browser, not its pod, calls the API and Entra.
NO_AZURE_IDENTITY = {"Rollout/web"}


def _build(path: Path) -> list[dict[str, Any]]:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl is required to build the GitOps overlays")
    out = subprocess.run([kubectl, "kustomize", str(path)], check=True,  # noqa: S603
                         capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


PLATFORM = _build(GITOPS / "overlays/dev")
WORKERS = _build(GITOPS / "overlays/dev/workers")  # its own Application, after Temporal
DEV = PLATFORM + WORKERS
# Every workload we own, deployed in dev or not: the worker bases too.
WEB = [d for d in PLATFORM if d["metadata"].get("namespace") == "platform-web"]  # ADR 0017
ALL_OURS = (PLATFORM + _build(GITOPS / "base/connector-servicenow")
            + _build(GITOPS / "base/worker-ai"))
TEMPORAL_APP = yaml.safe_load(
    (GITOPS / "applications/dev/temporal.yaml").read_text(encoding="utf-8"))
INTERNAL_CA = "cloudops-internal-ca"


def _workloads(docs: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    for d in docs:
        if d["kind"] in WORKLOAD_KINDS:
            yield f'{d["kind"]}/{d["metadata"]["name"]}', d["spec"]["template"]["spec"]


def test_dev_overlay_has_no_placeholder_left() -> None:
    assert "SET-IN-OVERLAY" not in yaml.safe_dump_all(DEV)


@pytest.mark.parametrize(("name", "pod"), list(_workloads(ALL_OURS)))
def test_every_pod_runs_restricted(name: str, pod: dict[str, Any]) -> None:
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True, name
    assert sc["seccompProfile"]["type"] == "RuntimeDefault", name
    assert pod.get("automountServiceAccountToken") is False, name
    for c in pod["containers"]:
        csc = c["securityContext"]
        assert csc["allowPrivilegeEscalation"] is False, name
        assert csc["readOnlyRootFilesystem"] is True, name
        assert csc["capabilities"]["drop"] == ["ALL"], name


@pytest.mark.parametrize(("name", "pod"), list(_workloads(ALL_OURS)))
def test_every_container_has_requests_and_a_memory_limit(name: str, pod: dict[str, Any]) -> None:
    for c in pod["containers"]:
        r = c["resources"]
        assert {"cpu", "memory"} <= r["requests"].keys(), name
        assert "memory" in r["limits"], name


def test_the_api_has_no_cpu_limit() -> None:
    """Latency-sensitive (LLD): CPU throttling shows up as p95 latency."""
    (pod,) = [p for n, p in _workloads(DEV) if n == "Rollout/api"]
    assert "cpu" not in pod["containers"][0]["resources"]["limits"]


@pytest.mark.parametrize(("name", "pod"), list(_workloads(ALL_OURS)))
def test_every_pod_is_pinned_to_its_pool(name: str, pod: dict[str, Any]) -> None:
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    (expr,) = terms[0]["matchExpressions"]
    assert expr["key"] == "cloudops.getronics.com/pool", name
    assert len(expr["values"]) == 1
    assert expr["values"][0] in POOLS, name


@pytest.mark.parametrize(("name", "pod"), list(_workloads(ALL_OURS)))
def test_secrets_are_files_never_environment(name: str, pod: dict[str, Any]) -> None:
    for c in pod["containers"]:
        for env in c.get("env", []):
            assert "secretKeyRef" not in env.get("valueFrom", {}), name
        for src in c.get("envFrom", []):
            assert "secretRef" not in src, name
    # Key Vault through the CSI driver only. The one exception is a certificate
    # cert-manager issues in-cluster (ADR 0010): its key never exists anywhere else.
    issued = {d["spec"]["secretName"] for d in ALL_OURS if d["kind"] == "Certificate"}
    for v in pod.get("volumes", []):
        if "secret" in v:
            assert v["secret"]["secretName"] in issued, name
        if "csi" in v:
            assert v["csi"]["driver"] == "secrets-store.csi.k8s.io"
            assert v["csi"]["readOnly"] is True


@pytest.mark.parametrize(("name", "pod"), list(_workloads(DEV)))
def test_settings_read_at_start_up_roll_the_pods_when_they_change(
    name: str, pod: dict[str, Any]
) -> None:
    # Environment from a ConfigMap is read once, at start-up: a plain ConfigMap edited
    # by Argo CD leaves running pods on the old value (the ServiceNow client ID,
    # 2026-10-31). A generated one is renamed by its content hash, which changes the
    # pod template and rolls the workload. Kept after pruning, so the previous
    # revision's pods can still start.
    configs = {d["metadata"]["name"]: d for d in DEV if d["kind"] == "ConfigMap"}
    for c in pod["containers"]:
        for src in c.get("envFrom", []):
            ref = src.get("configMapRef", {}).get("name")
            if ref is None:
                continue
            assert ref in configs, f"{name} reads ConfigMap {ref}, which the build lacks"
            notes = configs[ref]["metadata"].get("annotations", {})
            assert re.search(r"-[a-z0-9]{10}$", ref), f"{name}: {ref} is not generated"
            assert notes.get("argocd.argoproj.io/sync-options") == "Prune=false", ref
            assert notes.get("argocd.argoproj.io/compare-options") == "IgnoreExtraneous", ref


def _account(name: str, pod: dict[str, Any]) -> dict[str, Any]:
    accounts = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in ALL_OURS
                if d["kind"] == "ServiceAccount"}
    ns = next(d["metadata"]["namespace"] for d in ALL_OURS
              if f'{d["kind"]}/{d["metadata"]["name"]}' == name)
    return accounts[(ns, pod["serviceAccountName"])]


def test_every_workload_has_its_own_federated_identity() -> None:
    for name, pod in _workloads(ALL_OURS):
        if name in NO_AZURE_IDENTITY:
            continue
        sa = _account(name, pod)
        assert sa["metadata"]["annotations"]["azure.workload.identity/client-id"], name
        assert sa.get("automountServiceAccountToken") is False, name


def test_a_workload_without_azure_identity_has_none_at_all() -> None:
    """No client id, no injection, no Key Vault mount: nothing to exchange for a token."""
    found = {name for name, _ in _workloads(ALL_OURS)} & NO_AZURE_IDENTITY
    assert found == NO_AZURE_IDENTITY
    for d in ALL_OURS:
        name = f'{d["kind"]}/{d["metadata"]["name"]}'
        if name not in NO_AZURE_IDENTITY:
            continue
        pod = d["spec"]["template"]["spec"]
        sa = _account(name, pod)
        assert not sa["metadata"].get("annotations"), name
        assert sa.get("automountServiceAccountToken") is False, name
        labels = d["spec"]["template"]["metadata"].get("labels", {})
        assert "azure.workload.identity/use" not in labels, name
        assert not [v for v in pod.get("volumes", []) if "csi" in v or "secret" in v], name


def test_every_pod_gets_its_workload_identity_injected() -> None:
    """Without the label the webhook injects nothing, and the Azure profile refuses
    to start: a pod that always fails, found only in the cluster."""
    for d in ALL_OURS:
        if d["kind"] in WORKLOAD_KINDS and f'{d["kind"]}/{d["metadata"]["name"]}' \
                not in NO_AZURE_IDENTITY:
            labels = d["spec"]["template"]["metadata"].get("labels", {})
            assert labels.get("azure.workload.identity/use") == "true", d["metadata"]["name"]


def test_federated_accounts_match_terraform() -> None:
    """Stack 05 federates exactly the service accounts that carry a client id."""
    tf = (REPO_ROOT / "infra/terraform/stacks/05-aks/main.tf").read_text(encoding="utf-8")
    block = tf[tf.index("federated_service_accounts = {"):]
    block = block[:block.index("}")]
    federated = set(re.findall(r'= "([a-z0-9-]+/[a-z0-9-]+)"', block))
    declared = {f'{d["metadata"]["namespace"]}/{d["metadata"]["name"]}' for d in ALL_OURS
                if d["kind"] == "ServiceAccount"
                and "azure.workload.identity/client-id" in (d["metadata"].get("annotations") or {})}
    assert federated == declared


def test_multi_replica_workloads_have_a_pdb() -> None:
    pdbs = [d for d in ALL_OURS if d["kind"] == "PodDisruptionBudget"]
    for d in ALL_OURS:
        if d["kind"] in {"Deployment", "Rollout"}:
            scaled = any(s["kind"] in {"HorizontalPodAutoscaler", "ScaledObject"}
                         and s["metadata"]["name"] == d["metadata"]["name"] for s in ALL_OURS)
            if d["spec"].get("replicas", 1) > 1 or scaled:
                labels = d["spec"]["selector"]["matchLabels"]
                assert any(p["spec"]["selector"]["matchLabels"] == labels for p in pdbs), \
                    d["metadata"]["name"]


def test_every_namespace_denies_by_default() -> None:
    namespaces = {d["metadata"]["name"] for d in DEV if d["kind"] == "Namespace"}
    assert namespaces == OUR_NAMESPACES
    for ns in OUR_NAMESPACES:
        (deny,) = [d for d in DEV if d["kind"] == "NetworkPolicy"
                   and d["metadata"]["namespace"] == ns and d["metadata"]["name"] == "default-deny"]
        assert deny["spec"]["podSelector"] == {}
        assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        assert "ingress" not in deny["spec"]
        assert "egress" not in deny["spec"]


def test_images_come_only_from_the_platform_registry_by_tag() -> None:
    for name, pod in _workloads(DEV):
        for c in pod["containers"]:
            image = c["image"]
            assert re.match(r"crcloudops[a-z]+\.azurecr\.io/[a-z-]+:[0-9a-z.-]+$", image), name
            assert not image.endswith(":latest"), name


@pytest.mark.parametrize("name", ["api", "web"])
def test_releases_are_canaries_with_analysis(name: str) -> None:
    (rollout,) = [d for d in ALL_OURS if d["kind"] == "Rollout" and d["metadata"]["name"] == name]
    canary = rollout["spec"]["strategy"]["canary"]
    assert canary["steps"] == [{"setWeight": 10}, {"pause": {"duration": "15m"}}]
    assert canary["analysis"]["templates"] == [{"templateName": f"{name}-canary-health"}]
    (template,) = [d for d in ALL_OURS if d["kind"] == "AnalysisTemplate"
                   and d["metadata"]["name"] == f"{name}-canary-health"]
    assert {m["name"] for m in template["spec"]["metrics"]} == {"error-rate", "p95-latency"}


def _wave(d: dict[str, Any]) -> int:
    return int((d["metadata"].get("annotations") or {}).get("argocd.argoproj.io/sync-wave", 0))


def test_migrations_run_before_the_rollout() -> None:
    """Namespaces, then the job's own objects, then the job (a hook Argo CD waits on),
    then the Rollout. Not PreSync: that runs before the namespace and the job's
    ServiceAccount exist, so the job could never start on a fresh cluster."""
    (job,) = [d for d in PLATFORM if d["kind"] == "Job" and d["metadata"]["name"] == "migrate"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    (rollout,) = [d for d in PLATFORM if d["kind"] == "Rollout" and d["metadata"]["name"] == "api"]
    assert _wave(rollout) > _wave(job)
    sa_name = job["spec"]["template"]["spec"]["serviceAccountName"]
    needs = [d for d in PLATFORM if d["metadata"]["name"] in {sa_name, "migrate"}
             and d["kind"] in {"ServiceAccount", "ConfigMap", "NetworkPolicy"}]
    assert len(needs) == 3
    assert all(_wave(d) < _wave(job) for d in needs)
    for ns in (d for d in PLATFORM if d["kind"] == "Namespace"):
        assert _wave(ns) < min(_wave(d) for d in needs)
    assert "argocd.argoproj.io/hook" not in (needs[0]["metadata"].get("annotations") or {})
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["CLOUDOPS_MIGRATION_AUTH"] == "entra"
    (cm,) = [d for d in PLATFORM if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "migrate"]
    assert ":" not in cm["data"]["dsn"].split("//", 1)[1].split("@", 1)[0]  # no password


def test_applications_pin_their_charts_and_paths() -> None:
    apps = [yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in sorted((GITOPS / "applications/dev").glob("*.yaml"))]
    assert {a["metadata"]["name"] for a in apps} == {"argo-rollouts", "cert-manager",
                                                      "prometheus", "platform", "temporal",
                                                      "workers"}
    for app in apps:
        source = app["spec"]["source"]
        if "chart" in source:
            assert re.fullmatch(r"v?\d+\.\d+\.\d+", source["targetRevision"]), source["chart"]
        else:
            assert (REPO_ROOT / source["path"]).is_dir()
        assert "argocd.argoproj.io/sync-wave" in app["metadata"]["annotations"]


def test_the_first_administrator_is_bootstrapped_once_after_the_migrations() -> None:
    """ADR 0002 D14, ADR 0017: a Sync hook after the migration job, idempotent
    (--once), reading the administrator's oid from Key Vault and nothing else."""
    jobs = {d["metadata"]["name"]: d for d in PLATFORM if d["kind"] == "Job"}
    job = jobs["bootstrap-admin"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    assert _wave(job) > _wave(jobs["migrate"])
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["command"][-2:] == ["cloudops_api.bootstrap", "--once"]
    assert "args" not in container  # the oid is never on the command line
    (volume,) = pod["volumes"]
    spc = _one("SecretProviderClass", volume["csi"]["volumeAttributes"]["secretProviderClass"])
    objects = yaml.safe_load(spc["spec"]["parameters"]["objects"])["array"]
    names = {yaml.safe_load(o)["objectName"] for o in objects}
    assert names == {"postgres-app-dsn", "platform-admin-oid"}
    assert "platform-admin-oid" not in yaml.safe_dump(_one("SecretProviderClass", "api"))
    policy = _one("NetworkPolicy", "bootstrap-admin")
    assert [r["ports"] for r in policy["spec"]["egress"]] == [[{"protocol": "TCP", "port": 5432}]]


def _mapping_approvers(docs: list[dict[str, Any]]) -> str | None:
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "migrate"]
    value = cm.get("data", {}).get("mapping_approvers")
    return None if value is None else str(value)


def test_only_dev_lets_one_person_approve_a_customer_mapping() -> None:
    """ADR 0015: the database defaults to two people; dev alone says one."""
    (job,) = [d for d in PLATFORM if d["kind"] == "Job" and d["metadata"]["name"] == "migrate"]
    (ref,) = [e["valueFrom"]["configMapKeyRef"]
              for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
              if e["name"] == "CLOUDOPS_MAPPING_APPROVERS"]
    assert ref == {"name": "migrate", "key": "mapping_approvers", "optional": True}
    assert _mapping_approvers(PLATFORM) == "1"
    for env in ("uat", "prd"):
        assert _mapping_approvers(_build(GITOPS / "overlays" / env)) in {None, "2"}, env


@pytest.mark.parametrize("env", ["uat", "prd"])
def test_other_overlays_build(env: str) -> None:
    assert _build(GITOPS / "overlays" / env)


def test_autoscalers_come_after_what_they_scale() -> None:
    """An HPA whose target does not exist yet is Degraded, and Argo CD waits for a
    sync wave to be healthy before starting the next: the target would never come."""
    for hpa in (d for d in ALL_OURS if d["kind"] in {"HorizontalPodAutoscaler", "ScaledObject"}):
        ref = hpa["spec"]["scaleTargetRef"]
        kind = ref.get("kind", "Deployment")  # KEDA's default
        (target,) = [d for d in ALL_OURS if d["kind"] == kind
                     and d["metadata"]["name"] == ref["name"]]
        assert _wave(hpa) > _wave(target), hpa["metadata"]["name"]


def _app_wave(name: str) -> int:
    app = yaml.safe_load((GITOPS / f"applications/dev/{name}.yaml").read_text(encoding="utf-8"))
    return _wave(app)


def test_applications_start_in_dependency_order() -> None:
    """cert-manager's CRDs, then the CA and Temporal's certificates (platform), then
    Temporal, then the workers that need it to be ready."""
    assert _app_wave("cert-manager") < _app_wave("platform") < _app_wave("temporal") \
        < _app_wave("workers")


# Temporal mutual TLS (ADR 0010) -------------------------------------------------------


def test_every_certificate_comes_from_the_internal_ca_with_a_fresh_key() -> None:
    certs = [d for d in ALL_OURS if d["kind"] == "Certificate" and not d["spec"].get("isCA")]
    assert {c["metadata"]["name"] for c in certs} >= {
        "temporal-frontend", "temporal-internode", "temporal-web",
        "connector-servicenow-temporal", "worker-ai-temporal"}
    for c in certs:
        spec, name = c["spec"], c["metadata"]["name"]
        assert spec["issuerRef"] == {"group": "cert-manager.io", "kind": "ClusterIssuer",
                                     "name": INTERNAL_CA}, name
        assert spec["privateKey"]["rotationPolicy"] == "Always", name
        assert spec["privateKey"]["encoding"] == "PKCS8", name  # the Python SDK's rustls
        assert spec["duration"] == "2160h", name
        if name not in {"temporal-frontend", "temporal-internode"}:
            assert spec["usages"] == ["client auth"], name  # a client cannot serve


def test_the_ca_is_never_pruned_and_keeps_its_key() -> None:
    (ca,) = [d for d in DEV if d["kind"] == "Certificate" and d["spec"].get("isCA")]
    assert ca["metadata"]["name"] == ca["spec"]["secretName"] == INTERNAL_CA
    assert ca["metadata"]["namespace"] == "cert-manager"
    assert ca["spec"]["privateKey"]["rotationPolicy"] == "Never"
    assert "Prune=false" in ca["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
    issuers = {d["metadata"]["name"]: d for d in DEV if d["kind"] == "ClusterIssuer"}
    assert issuers[INTERNAL_CA]["spec"] == {"ca": {"secretName": INTERNAL_CA}}
    assert _wave(issuers[INTERNAL_CA]) > _wave(ca)
    for cert in (d for d in DEV if d["kind"] == "Certificate" and not d["spec"].get("isCA")):
        assert _wave(cert) > _wave(issuers[INTERNAL_CA]), cert["metadata"]["name"]


@pytest.mark.parametrize(("name", "pod"), list(_workloads(ALL_OURS)))
def test_every_temporal_client_presents_a_certificate(name: str, pod: dict[str, Any]) -> None:
    configs = {d["metadata"]["name"]: d["data"] for d in ALL_OURS if d["kind"] == "ConfigMap"}
    for c in pod["containers"]:
        env = {}
        for src in c.get("envFrom", []):
            env.update(configs.get(src.get("configMapRef", {}).get("name", ""), {}))
        if "CLOUDOPS_TEMPORAL_ADDRESS" not in env:
            continue
        tls_dir = env["CLOUDOPS_TEMPORAL_TLS_DIR"]
        (mount,) = [m for m in c["volumeMounts"] if m["mountPath"] == tls_dir]
        assert "subPath" not in mount, name  # renewed files must appear in place
        (volume,) = [v for v in pod["volumes"] if v["name"] == mount["name"]]
        assert volume["secret"]["secretName"].endswith("-temporal-tls"), name


def test_every_temporal_scaler_authenticates() -> None:
    auths = {d["metadata"]["name"]: d for d in ALL_OURS if d["kind"] == "TriggerAuthentication"}
    certs = {d["spec"]["secretName"] for d in ALL_OURS if d["kind"] == "Certificate"}
    for so in (d for d in ALL_OURS if d["kind"] == "ScaledObject"):
        for trigger in so["spec"]["triggers"]:
            if trigger["type"] != "temporal":
                continue
            auth = auths[trigger["authenticationRef"]["name"]]
            refs = auth["spec"]["secretTargetRef"]
            assert {r["parameter"] for r in refs} == {"cert", "key", "ca"}
            assert {r["name"] for r in refs} <= certs
            assert "unsafeSsl" not in trigger["metadata"]
            host = trigger["metadata"]["endpoint"].rsplit(":", 1)[0]
            assert trigger["metadata"]["tlsServerName"] == host


def test_temporal_requires_client_certificates_everywhere() -> None:
    values = TEMPORAL_APP["spec"]["source"]["helm"]["valuesObject"]
    tls = values["server"]["tls"]
    for section in ("frontend", "internode"):
        assert tls[section]["enabled"] is True, section
        assert tls[section]["requireClientAuth"] is True, section
    issued = {d["spec"]["secretName"] for d in DEV if d["kind"] == "Certificate"}
    assert {tls["frontend"]["secretName"], tls["internode"]["secretName"]} <= issued
    web = values["web"]["tls"]
    assert web["enabled"] is True
    assert web["enableHostVerification"] is True
    assert web["secretName"] in issued
    assert values["server"]["config"]["tls"]["refreshInterval"]


# Third-party images: mirrored into ACR, pinned by digest (ADR 0008) ------------------

MIRRORED = {
    "infra/gitops/applications/dev/temporal.yaml": ["server.image", "admintools.image",
                                                    "web.image"],
    "infra/gitops/applications/dev/cert-manager.yaml": ["image", "webhook.image",
                                                        "cainjector.image"],
    "infra/gitops/applications/dev/argo-rollouts.yaml": ["controller.image",
                                                         "controller.initContainers"],
    "infra/gitops/applications/dev/prometheus.yaml": ["server.image",
                                                      "configmapReload.prometheus.image"],
    "infra/cluster/argocd/values.yaml": ["global.image", "redis.image"],
}


def _values(path: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
    if path.startswith("infra/gitops/applications/"):
        values: dict[str, Any] = doc["spec"]["source"]["helm"]["valuesObject"]
        return values
    return doc


@pytest.mark.parametrize(("path", "key"),
                         [(p, k) for p, keys in MIRRORED.items() for k in keys])
def test_third_party_images_come_from_acr_by_digest(path: str, key: str) -> None:
    """Every image a chart would otherwise pull from a public registry is set here;
    dropping one silently falls back to the chart's default registry."""
    values = _values(path)
    image = values
    for part in key.split("."):
        image = image[part]
    text = yaml.safe_dump(image)
    registry = values.get("imageRegistry", "") if path.endswith("cert-manager.yaml") else text
    assert re.search(r"crcloudops[a-z]+\.azurecr\.io", registry), (path, key)
    assert re.search(r"sha256:[0-9a-f]{64}", text), (path, key)


# The Gateway API ingress (ADR 0011) -------------------------------------------------


def _one(kind: str, name: str) -> dict[str, Any]:
    (doc,) = [d for d in DEV if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


def test_the_gateway_admits_only_front_door_and_only_the_api_and_console_namespaces() -> None:
    gateway = _one("Gateway", "api")
    assert gateway["metadata"]["namespace"] == "platform-edge"
    assert gateway["spec"]["gatewayClassName"] == "approuting-istio"
    annotations = gateway["spec"]["infrastructure"]["annotations"]
    assert annotations["service.beta.kubernetes.io/azure-allowed-service-tags"] \
        == "AzureFrontDoor.Backend"  # lock 1
    assert annotations["service.beta.kubernetes.io/azure-pip-name"].startswith("pip-cloudops-")
    for listener in gateway["spec"]["listeners"]:
        allowed = listener["allowedRoutes"]["namespaces"]
        assert allowed["from"] == "Selector"
        assert allowed["selector"] == {"matchExpressions": [{
            "key": "kubernetes.io/metadata.name", "operator": "In",
            "values": ["platform-api", "platform-web"]}]}


def test_every_api_route_but_the_probe_requires_our_front_door() -> None:
    """Lock 2: the NSG's service tag admits every Front Door profile in Azure, so the
    route must also match our profile's X-Azure-FDID; only /healthz is open."""
    route = _one("HTTPRoute", "api")
    values = _one_values()
    for rule in route["spec"]["rules"]:
        for match in rule["matches"]:
            if match["path"] == {"type": "Exact", "value": "/healthz"}:
                assert "headers" not in match
                continue
            headers = {h["name"].lower(): h for h in match.get("headers", [])}
            fdid = headers["x-azure-fdid"]
            assert fdid["type"] == "Exact"
            assert fdid["value"] == values["frontdoorId"]
        # The Rollouts plugin moves weight between these two; both must be present.
        assert {b["name"] for b in rule["backendRefs"]} == {"api", "api-canary"}


def test_every_console_route_requires_our_front_door() -> None:
    """The console's route has no open path at all: Front Door probes the API."""
    (route,) = [d for d in WEB if d["kind"] == "HTTPRoute"]
    assert route["metadata"]["namespace"] == "platform-web"
    for rule in route["spec"]["rules"]:
        for match in rule["matches"]:
            headers = {h["name"].lower(): h for h in match.get("headers", [])}
            assert headers["x-azure-fdid"]["type"] == "Exact"
            assert headers["x-azure-fdid"]["value"] == _one_values()["frontdoorId"]
        assert {b["name"] for b in rule["backendRefs"]} == {"web", "web-canary"}


def test_the_api_keeps_its_paths_on_the_shared_host() -> None:
    """One host, two routes (ADR 0017): the API's matches are more specific than the
    console's catch-all, so /v1 and the probe always reach the API."""
    (web,) = [d for d in WEB if d["kind"] == "HTTPRoute"]
    (path,) = [m["path"] for r in web["spec"]["rules"] for m in r["matches"]]
    assert path == {"type": "PathPrefix", "value": "/"}
    api_paths = [m["path"] for r in _one("HTTPRoute", "api")["spec"]["rules"] for m in r["matches"]]
    assert {"type": "Exact", "value": "/healthz"} in api_paths
    assert {"type": "PathPrefix", "value": "/v1"} in api_paths


def test_the_console_is_reached_from_the_ingress_and_reaches_nothing() -> None:
    (policy,) = [d for d in WEB if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "web"]
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "egress" not in policy["spec"]  # DNS only, from allow-dns
    (rule,) = policy["spec"]["ingress"]
    (peer,) = rule["from"]
    assert peer["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "platform-edge"}
    assert peer["podSelector"]["matchLabels"] == {"gateway.networking.k8s.io/gateway-name": "api"}
    gateway = _one("NetworkPolicy", "api-gateway")
    ports = {(p["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
              p["ports"][0]["port"]) for p in gateway["spec"]["egress"]}
    assert ("platform-web", 3000) in ports


def test_the_console_reads_only_identifiers() -> None:
    (cm,) = [d for d in WEB if d["kind"] == "ConfigMap"]
    assert set(cm["data"]) == {"CLOUDOPS_WEB_API_BASE", "CLOUDOPS_WEB_ENTRA_CLIENT_ID",
                               "CLOUDOPS_WEB_ENTRA_TENANT_ID", "CLOUDOPS_WEB_API_SCOPE"}
    data = cm["data"]
    assert data["CLOUDOPS_WEB_API_BASE"].startswith("https://")
    assert re.fullmatch(r"api://[0-9a-f-]{36}/access_as_user", data["CLOUDOPS_WEB_API_SCOPE"])


def test_the_console_and_the_api_agree_on_the_origin() -> None:
    """Same host (ADR 0017): the API base the console calls is the origin the API's
    CORS names, and the API's audience is the scope's resource."""
    (web,) = [d for d in WEB if d["kind"] == "ConfigMap"]
    (api,) = [d for d in DEV if d["kind"] == "ConfigMap"
              and d["metadata"]["name"].startswith("api-")
              and "CLOUDOPS_API_AUDIENCE" in d.get("data", {})]
    assert api["data"]["CLOUDOPS_WEB_ORIGINS"] == web["data"]["CLOUDOPS_WEB_API_BASE"]
    scope_resource = web["data"]["CLOUDOPS_WEB_API_SCOPE"].split("/")[2]
    assert scope_resource == api["data"]["CLOUDOPS_API_AUDIENCE"]


def test_the_api_allows_only_https_console_origins() -> None:
    (cm,) = [d for d in DEV if d["kind"] == "ConfigMap" and d["metadata"]["name"].startswith("api-")
             and "CLOUDOPS_API_AUDIENCE" in d.get("data", {})]
    origins = cm["data"]["CLOUDOPS_WEB_ORIGINS"].split(",")
    assert origins == ["https://cloudops-dev-dne7dpdpfrgfccej.z02.azurefd.net"]


def _one_values() -> dict[str, str]:
    values = yaml.safe_load((GITOPS / "overlays/dev/values.yaml").read_text(encoding="utf-8"))
    data: dict[str, str] = values["data"]
    assert re.fullmatch(r"[0-9a-f-]{36}", data["frontdoorId"])
    return data


def test_only_the_ingress_reaches_the_api() -> None:
    policy = _one("NetworkPolicy", "api")
    sources = [peer for rule in policy["spec"]["ingress"] for peer in rule["from"]]
    allowed = {peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
               for peer in sources}
    assert allowed == {"platform-edge"}
    (edge,) = sources
    assert edge["podSelector"]["matchLabels"] == {
        "gateway.networking.k8s.io/gateway-name": "api"}


def test_ingress_nginx_is_gone_for_good() -> None:
    """Retired upstream, never patched again (ADR 0011, phase C): no Ingress object,
    no policy naming its namespace, and the bootstrap never installs it again."""
    everything = ALL_OURS + WORKERS
    assert not [d for d in everything if d["kind"] == "Ingress"]
    assert "ingress-nginx" not in yaml.safe_dump_all(everything)
    bootstrap = (REPO_ROOT / "scripts/bootstrap-cluster.sh").read_text(encoding="utf-8")
    assert "ingress-nginx" not in bootstrap
    assert not (REPO_ROOT / "infra/cluster/ingress-nginx").exists()


def test_the_rollouts_plugin_is_local_and_pinned() -> None:
    values = _values("infra/gitops/applications/dev/argo-rollouts.yaml")
    (plugin,) = values["controller"]["trafficRouterPlugins"]
    assert plugin["name"] == "argoproj-labs/gatewayAPI"
    assert plugin["location"].startswith("file:///plugins/")  # never fetched at runtime
    assert re.fullmatch(r"[0-9a-f]{64}", plugin["sha256"])



def test_canaries_route_through_the_gateway_and_are_judged_on_its_metrics() -> None:
    """Phase B (ADR 0011): the plugin owns the weights, Argo CD ignores exactly them,
    and the analysis reads the gateway's view of the canary Service only."""
    rollout = _one("Rollout", "api")
    routing = rollout["spec"]["strategy"]["canary"]["trafficRouting"]
    assert routing == {"plugins": {"argoproj-labs/gatewayAPI": {
        "httpRoute": "api", "namespace": "platform-api"}}}
    template = _one("AnalysisTemplate", "api-canary-health")
    for metric in template["spec"]["metrics"]:
        query = metric["provider"]["prometheus"]["query"]
        assert 'destination_service_name="api-canary"' in query, metric["name"]
        assert 'reporter="source"' in query, metric["name"]
        assert "nginx" not in query, metric["name"]
        # Empty is an error to Rollouts, not a pass: every query must fall back.
        assert re.search(r"or vector\((0|NaN)\)", query), metric["name"]
    (p95,) = [m for m in template["spec"]["metrics"] if m["name"] == "p95-latency"]
    assert "milliseconds" in p95["provider"]["prometheus"]["query"]
    assert p95["successCondition"].endswith("< 500")  # Istio reports milliseconds
    app = yaml.safe_load((GITOPS / "applications/dev/platform.yaml").read_text(encoding="utf-8"))
    ignored = app["spec"]["ignoreDifferences"]
    assert {(i["kind"], i["namespace"], i["name"]) for i in ignored} == {
        ("HTTPRoute", "platform-api", "api"), ("HTTPRoute", "platform-web", "web")}
    for i in ignored:
        assert i["jqPathExpressions"][0] == ".spec.rules[].backendRefs[].weight"
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]



def test_front_door_only_reaches_nodes_that_run_the_gateway() -> None:
    """Only the app subnet's NSG admits Front Door. With externalTrafficPolicy Cluster
    the load balancer also sends to system nodes, whose NSG drops the connection."""
    config = _one("ConfigMap", "api-gateway")["data"]
    service = yaml.safe_load(config["service"])
    assert service["spec"]["externalTrafficPolicy"] == "Local"
    unset = service["metadata"]["annotations"]
    assert all(v is None for v in unset.values())
    deployment = yaml.safe_load(config["deployment"])
    assert deployment["spec"]["template"]["spec"]["nodeSelector"] == {
        "cloudops.getronics.com/pool": "app"}
