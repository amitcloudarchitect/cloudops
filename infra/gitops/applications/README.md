# Argo CD applications

One directory per environment. The root Application
(`infra/cluster/argocd/root-application.yaml.tpl`, installed by
`scripts/bootstrap-cluster.sh --env <env>`) deploys every Application in
`applications/<env>/`, which in turn deploy the charts and the Kustomize overlay.

Order (sync waves): cert-manager (-3, its CRDs and the CA come first), Argo Rollouts
(-2, its CRDs are needed by the platform), Prometheus (-1), the platform overlay (0),
Temporal (1), the workers (2, `overlays/<env>/workers`; not ready until Temporal
answers). Inside the platform overlay, its own waves run namespaces and the internal
CA, then Temporal's certificates, then the migration job, then the API Rollout.

The pipeline (step 13) changes image tags in `overlays/<env>`; Argo CD pulls and
reconciles. Nothing pushes to the cluster.
