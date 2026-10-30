#!/usr/bin/env bash
# Bootstrap a freshly applied AKS cluster: credentials, Argo CD and the root
# Application. Idempotent: every step is an upsert, so re-running it converges. The
# ingress is the AKS Gateway API add-on (stack 05) with its Gateway in the GitOps tree
# (ADR 0011), so nothing here installs one.
#
#   scripts/bootstrap-cluster.sh --repo-url https://github.com/<org>/<repo>.git [--revision main] [--env dev]
#
# Needs: az (signed in), kubectl, kubelogin, helm, envsubst, terraform, and stacks 01,
# 05 and 06-edge applied (their outputs are read here). Run it from the repository root.
# It creates no credential: AKS is Entra-only, and a private repository's credentials
# are registered with Argo CD separately (step 12).
set -euo pipefail

ARGO_CD_CHART_VERSION=10.11.6
ENV_DIR=infra/terraform/env/dev
STACKS=infra/terraform/stacks
CLUSTER=infra/cluster

repo_url="" revision="main" env="dev"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url) repo_url="$2"; shift 2 ;;
    --revision) revision="$2"; shift 2 ;;
    --env) env="$2"; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$repo_url" ]] || { echo "--repo-url is required (the repository Argo CD pulls from)" >&2; exit 2; }
[[ "$repo_url" =~ ^https:// ]] || { echo "--repo-url must be https" >&2; exit 2; }
[[ -d "infra/gitops/applications/$env" ]] || { echo "no infra/gitops/applications/$env" >&2; exit 2; }

for tool in az kubectl kubelogin helm envsubst terraform; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
az account show >/dev/null 2>&1 || { echo "run az login first" >&2; exit 1; }

tf_output() {  # stack, output name
  terraform -chdir="$STACKS/$1" output -raw "$2"
}
for stack in 05-aks 06-edge; do
  if [[ ! -d "$STACKS/$stack/.terraform" ]]; then
    terraform -chdir="$STACKS/$stack" init -input=false -backend-config="../../env/dev/backend.hcl" >/dev/null
  fi
done

echo "==> Cluster credentials"
eval "$(tf_output 05-aks get_credentials_command)"
kubectl get nodes -o wide >/dev/null

frontdoor_hostname="$(tf_output 06-edge frontdoor_hostname)"

render() {  # template; substitutes only the named variables
  REPO_URL="$repo_url" REVISION="$revision" ENV="$env" \
    envsubst '${REPO_URL} ${REVISION} ${ENV}' < "$1"
}

echo "==> Argo CD chart $ARGO_CD_CHART_VERSION"
helm upgrade --install argocd argo-cd \
  --repo https://argoproj.github.io/argo-helm --version "$ARGO_CD_CHART_VERSION" \
  --namespace argocd --create-namespace \
  --values "$CLUSTER/argocd/values.yaml" --wait --timeout 10m
render "$CLUSTER/argocd/root-application.yaml.tpl" | kubectl apply -f -

echo
echo "Done. Front Door: https://$frontdoor_hostname (the origin is unhealthy until the API is deployed)"
echo
echo "Argo CD initial admin password (change it, then delete the secret):"
if kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then
  echo "  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
else
  echo "  (already deleted; use the password you set)"
fi
echo
echo "Argo CD UI:"
echo "  kubectl -n argocd port-forward svc/argocd-server 8080:443   # then https://localhost:8080, user admin"
