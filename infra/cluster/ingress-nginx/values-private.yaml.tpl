# ingress-nginx, network_mode = "private" (the LLD): an internal load balancer with a
# Private Link Service, which Front Door Premium reaches over Private Link. The
# connection needs a manual approval (MANUAL-STEPS.md). The FDID check stays: Private
# Link narrows the network path, the header still proves which profile is calling.
controller:
  replicaCount: 2
  nodeSelector:
    cloudops.getronics.com/pool: app
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
      service.beta.kubernetes.io/azure-load-balancer-internal: "true"
      service.beta.kubernetes.io/azure-pls-create: "true"
      service.beta.kubernetes.io/azure-load-balancer-health-probe-request-path: /healthz
  # Request metrics for Argo Rollouts' canary analysis (in-cluster Prometheus).
  metrics:
    enabled: true
  podAnnotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "10254"
  admissionWebhooks:
    enabled: true
