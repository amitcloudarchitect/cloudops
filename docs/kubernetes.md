# Kubernetes and GitOps

What runs in the cluster and how it gets there. Decisions: ADR 0008.

```
infra/gitops/
  applications/<env>/   Argo CD Applications (Rollouts, Prometheus, platform, Temporal)
  base/                 Kustomize bases: namespaces, api, migrate, temporal-secrets,
                        monitoring, connector-servicenow, worker-ai
  overlays/<env>/       Per-environment values and image tags (dev is the worked example)
```

`make check` builds every overlay and checks the pod controls
(`tools/ci/tests/test_gitops.py`).

## First deployment in dev, in order

Plan each stack and check the plan before applying. All are in place: nothing is
replaced or destroyed.

1. **Stack 01** adds the dev-only devbox-to-Key-Vault rule.
2. **Stack 04** turns on password login and `BTREE_GIN`. The break-glass password is
   generated, used once and not kept:
   ```bash
   TF_VAR_postgres_breakglass_password_wo="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" terraform apply -var-file=../../env/dev/common.tfvars -var-file=../../env/dev/04-data.tfvars
   ```
3. **Stack 05** adds the KEDA add-on and workload identity federation.
4. **Stack 04b** grants the workloads read access to the app vault, and you write
   access in dev.
5. **The database and its logins**, from the repository root on the devbox. This
   creates the databases, roles and extensions, and puts the connection strings in
   Key Vault:
   ```bash
   uv run python scripts/db_dev_logins.py
   ```
6. **Mirror the pause image** that holds Temporal's secret mount:
   ```bash
   az acr import -n crcloudopsdevweu --source registry.k8s.io/pause:3.10.1 --image pause:3.10.1
   ```
7. **Push the migrate image**, set its tag in `overlays/dev`, then commit and push.
8. **Re-run the bootstrap.** It points the root app at `applications/dev`, adds Argo CD's
   Application health check, and turns on ingress-nginx metrics:
   ```bash
   scripts/bootstrap-cluster.sh --repo-url https://github.com/amitcloudarchitect/cloudops.git --env dev
   ```

Argo CD then syncs, in order:
1. Rollouts;
2. Prometheus;
3. the platform: namespaces and policies, the migration job (Argo CD waits for it
   to complete; a failure stops the sync), then the API canary;
4. Temporal.

## Rotating dev database passwords

```bash
uv run python scripts/db_dev_logins.py --rotate
```

Then restart the pods that read them: `kubectl -n platform-api rollout restart rollout api`,
and the `temporal-secrets` and Temporal deployments.

## Looking at things

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443
```

```bash
kubectl -n temporal port-forward svc/temporal-web 8081:8080
```

```bash
kubectl argo rollouts -n platform-api get rollout api
```
(The last needs the Argo Rollouts kubectl plugin.)
