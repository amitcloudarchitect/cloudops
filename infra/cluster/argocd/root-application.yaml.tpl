# The root ("app of apps") Application. Rendered by scripts/bootstrap-cluster.sh with
# ${REPO_URL}, ${REVISION} and ${ENV}; everything under infra/gitops/applications/${ENV}
# is what Argo CD deploys from then on. A private repository needs credentials registered with Argo
# CD first (step 12): none are created or stored here.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ${REPO_URL}
    targetRevision: ${REVISION}
    path: infra/gitops/applications/${ENV}
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
