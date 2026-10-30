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


def test_every_workload_has_its_own_federated_identity() -> None:
    accounts = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in ALL_OURS
                if d["kind"] == "ServiceAccount"}
    for name, pod in _workloads(ALL_OURS):
        ns = next(d["metadata"]["namespace"] for d in ALL_OURS
                  if f'{d["kind"]}/{d["metadata"]["name"]}' == name)
        sa = accounts[(ns, pod["serviceAccountName"])]
        assert sa["metadata"]["annotations"]["azure.workload.identity/client-id"], name
        assert sa.get("automountServiceAccountToken") is False, name


def test_every_pod_gets_its_workload_identity_injected() -> None:
    """Without the label the webhook injects nothing, and the Azure profile refuses
    to start: a pod that always fails, found only in the cluster."""
    for d in ALL_OURS:
        if d["kind"] in WORKLOAD_KINDS:
            labels = d["spec"]["template"]["metadata"].get("labels", {})
            assert labels.get("azure.workload.identity/use") == "true", d["metadata"]["name"]


def test_federated_accounts_match_terraform() -> None:
    """Stack 05 federates exactly the service accounts the bases declare."""
    tf = (REPO_ROOT / "infra/terraform/stacks/05-aks/main.tf").read_text(encoding="utf-8")
    block = tf[tf.index("federated_service_accounts = {"):]
    block = block[:block.index("}")]
    federated = set(re.findall(r'= "([a-z0-9-]+/[a-z0-9-]+)"', block))
    declared = {f'{d["metadata"]["namespace"]}/{d["metadata"]["name"]}' for d in ALL_OURS
                if d["kind"] == "ServiceAccount"}
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


def test_api_releases_by_canary_with_analysis() -> None:
    (rollout,) = [d for d in DEV if d["kind"] == "Rollout"]
    canary = rollout["spec"]["strategy"]["canary"]
    assert canary["steps"] == [{"setWeight": 10}, {"pause": {"duration": "15m"}}]
    assert canary["analysis"]["templates"] == [{"templateName": "api-canary-health"}]
    (template,) = [d for d in DEV if d["kind"] == "AnalysisTemplate"]
    assert {m["name"] for m in template["spec"]["metrics"]} == {"error-rate", "p95-latency"}


def _wave(d: dict[str, Any]) -> int:
    return int((d["metadata"].get("annotations") or {}).get("argocd.argoproj.io/sync-wave", 0))


def test_migrations_run_before_the_rollout() -> None:
    """Namespaces, then the job's own objects, then the job (a hook Argo CD waits on),
    then the Rollout. Not PreSync: that runs before the namespace and the job's
    ServiceAccount exist, so the job could never start on a fresh cluster."""
    (job,) = [d for d in PLATFORM if d["kind"] == "Job"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    (rollout,) = [d for d in PLATFORM if d["kind"] == "Rollout"]
    assert _wave(rollout) > _wave(job)
    sa_name = job["spec"]["template"]["spec"]["serviceAccountName"]
    needs = [d for d in PLATFORM if d["metadata"]["name"] in {sa_name, "migrate"}
             and d["kind"] in {"ServiceAccount", "ConfigMap", "NetworkPolicy"}]
    assert len(needs) == 3
    assert all(_wave(d) < _wave(job) for d in needs)
    for ns in (d for d in PLATFORM if d["kind"] == "Namespace"):
        assert _wave(ns) < min(_wave(d) for d in needs)
    assert "argocd.argoproj.io/hook" not in (needs[0]["metadata"].get("annotations") or {})
    env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
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
    "infra/cluster/ingress-nginx/values-public.yaml.tpl": [
        "controller.image", "controller.admissionWebhooks.patch.image"],
    "infra/cluster/ingress-nginx/values-private.yaml.tpl": [
        "controller.image", "controller.admissionWebhooks.patch.image"],
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


def test_the_gateway_admits_only_front_door_and_only_the_api_namespace() -> None:
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
        assert allowed["selector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "platform-api"}


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
    assert allowed == {"ingress-nginx", "platform-edge"}
    edge = [p for p in sources if p["namespaceSelector"]["matchLabels"][
        "kubernetes.io/metadata.name"] == "platform-edge"]
    assert edge[0]["podSelector"]["matchLabels"] == {
        "gateway.networking.k8s.io/gateway-name": "api"}


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
    (p95,) = [m for m in template["spec"]["metrics"] if m["name"] == "p95-latency"]
    assert "milliseconds" in p95["provider"]["prometheus"]["query"]
    assert p95["successCondition"].endswith("< 500")  # Istio reports milliseconds
    app = yaml.safe_load((GITOPS / "applications/dev/platform.yaml").read_text(encoding="utf-8"))
    (ignored,) = app["spec"]["ignoreDifferences"]
    assert (ignored["kind"], ignored["name"]) == ("HTTPRoute", "api")
    assert ignored["jqPathExpressions"][0] == ".spec.rules[].backendRefs[].weight"
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]
