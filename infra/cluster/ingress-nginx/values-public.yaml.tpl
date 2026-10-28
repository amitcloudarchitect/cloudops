# ingress-nginx, network_mode = "public" (dev). Rendered by scripts/bootstrap-cluster.sh,
# which substitutes only ${INGRESS_IP}, ${INGRESS_IP_RG} and ${FRONTDOOR_ID}.
#
# Two locks, both required (infra/terraform/stacks/06-edge explains why):
#   - stack 01's NSG admits only the AzureFrontDoor.Backend service tag;
#   - the server snippet below rejects any request whose X-Azure-FDID is not our
#     Front Door profile, because that service tag covers every profile in Azure.
controller:
  replicaCount: 2
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
  admissionWebhooks:
    enabled: true
