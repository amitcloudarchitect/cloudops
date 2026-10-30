# ingress-nginx, network_mode = "public" (dev). Rendered by scripts/bootstrap-cluster.sh,
# which substitutes only ${INGRESS_IP}, ${INGRESS_IP_RG} and ${FRONTDOOR_ID}.
#
# Two locks, both required (infra/terraform/stacks/06-edge explains why):
#   - stack 01's NSG admits only the AzureFrontDoor.Backend service tag;
#   - the server snippet below rejects any request whose X-Azure-FDID is not our
#     Front Door profile, because that service tag covers every profile in Azure.
controller:
  replicaCount: 2
  # Mirrored into ACR at the chart's own digests (ADR 0008, third-party images).
  image:
    registry: crcloudopsdevweu.azurecr.io
    image: ingress-nginx/controller
    tag: v1.15.1
    digest: sha256:594ceea76b01c592858f803f9ff4d2cb40542cae2060410b2c95f75907d659e1
  nodeSelector:
    cloudops.getronics.com/pool: app
  # Per-Ingress snippet annotations stay off: enabling them made the 2025 ingress-nginx
  # CVEs exploitable. The FDID check is cluster-operator configuration, below.
  allowSnippetAnnotations: false
  config:
    use-forwarded-headers: "true"
    server-tokens: "false"
    server-snippet: |
      set $cloudops_fd_ok 0;
      if ($http_x_azure_fdid = "${FRONTDOOR_ID}") { set $cloudops_fd_ok 1; }
      if ($uri = "/healthz") { set $cloudops_fd_ok 1; }
      if ($cloudops_fd_ok = 0) { return 403; }
  resources:
    requests: { cpu: 100m, memory: 128Mi }
    limits: { memory: 384Mi }
  service:
    externalTrafficPolicy: Local
    annotations:
      service.beta.kubernetes.io/azure-load-balancer-resource-group: "${INGRESS_IP_RG}"
      service.beta.kubernetes.io/azure-load-balancer-ipv4: "${INGRESS_IP}"
      # The cluster-managed NSG rule, too, admits Front Door only.
      service.beta.kubernetes.io/azure-allowed-service-tags: "AzureFrontDoor.Backend"
      service.beta.kubernetes.io/azure-load-balancer-health-probe-request-path: /healthz
  # Request metrics for Argo Rollouts' canary analysis (in-cluster Prometheus).
  metrics:
    enabled: true
  podAnnotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "10254"
  admissionWebhooks:
    patch:
      image:
        registry: crcloudopsdevweu.azurecr.io
        image: ingress-nginx/kube-webhook-certgen
        tag: v1.6.9
        digest: sha256:01038e7de14b78d702d2849c3aad72fd25903c4765af63cf16aa3398f5d5f2dd
    enabled: true
