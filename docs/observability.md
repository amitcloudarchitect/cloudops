# Observability: turning it on

Step 19, ADR 0021. The code ships with telemetry configured as `otlp` in every base;
dev's overlay says `off` until the collector can run there, because the collector
needs its own workload identity. These are the steps, in order. Nothing here is
applied by CI.

## 1. Terraform (plan, then you apply)

| Stack | What it adds | Notes |
| --- | --- | --- |
| `02-security` | `id-cloudops-observability-dev` | The collector's identity |
| `03-observability` | Azure Monitor workspace, Managed Grafana, action groups `ag-cloudops-page-dev` and `ag-cloudops-notify-dev`, three Sev0 security-event rules, the signing-key rule, the P2 and SLO Prometheus rule groups | Set `page_email_receivers` / `notify_email_receivers` (a stack tfvars file), and any on-call webhook only as `TF_VAR_page_webhook_receivers`. An action group without receivers pages nobody. |
| `04b-rbac` | Monitoring Metrics Publisher on Application Insights for the collector; Monitoring Data Reader on the workspace and Monitoring Reader on the observability resource group for Grafana; Grafana Admin and Viewer for the groups you name | `observability_enabled = true`, `grafana_admin_principal_ids`, `grafana_viewer_principal_ids` in `04b-rbac.tfvars` (and the CI variable `TF_DEV_04B_RBAC_TFVARS`). Needs User Access Administrator. |
| `05-aks` | Managed Prometheus on the cluster (`monitor_metrics`, the workspace's data collection rule and endpoint associated); federated credentials for `observability/otel-collector` and `platform-ingest/platform-metrics` | `observability_enabled = true` in `05-aks.tfvars` (and `TF_DEV_05_AKS_TFVARS`). The platform-metrics credential plans already. |
| `03b-dashboards` | Six dashboards and their Prometheus data source | Needs Grafana Admin and a token, below. Not in CI's plan matrix. |

```bash
export TF_VAR_grafana_token=$(az account get-access-token --resource ce34e7e5-485f-4d76-964f-b3d2b16d1e4f --query accessToken -o tsv)
```

(`ce34e7e5-…` is Azure Managed Grafana's well-known application id.)

### The break-glass alert

It reads `SigninLogs`, which reach the workspace only through a tenant-level Entra
diagnostic setting. An Entra administrator creates it (Entra ID → Diagnostic settings
→ send `SignInLogs` to `log-cloudops-dev-weu`). Then set, in `03-observability`'s
tfvars, `entra_signin_logs_exported = true` and `break_glass_object_ids` to the two
accounts' object ids.

## 2. The collector image

Mirror it into ACR, pinned by digest (ADR 0008):

```bash
az acr import -n crcloudopsdevweu --source docker.io/otel/opentelemetry-collector-contrib@sha256:39923a8e431bd1f57be82411999d389fcfe40857492e4365456d97a4c1f74be6 --image opentelemetry-collector-contrib:0.162.0
```

## 3. The dev overlay (a follow-up PR once 1 and 2 are applied)

- `overlays/dev/kustomization.yaml`: add `../../base/observability`, map the image
  `otel-collector` to `crcloudopsdevweu.azurecr.io/opentelemetry-collector-contrib:0.162.0`,
  patch `ServiceAccount/otel-collector` with the new identity's client id and
  `ConfigMap/otel-collector` with `APPLICATIONINSIGHTS_CONNECTION_STRING` (stack 03
  output `app_insights_connection_string`: not a secret with local authentication off).
- `overlays/dev/workers`: add `../../../base/platform-metrics` with the ingest
  identity's client id, the app vault, and the data subnet replacement.
- Remove the `CLOUDOPS_TELEMETRY: "off"` lines from `overlays/dev/config.yaml` and the
  two worker patches.
- `argo-rollouts` already exposes its metrics; the scrape job for it is in
  `base/observability/ama-metrics-prometheus-config.yaml`.

## What you get

- Traces in Application Insights: every API request, every Temporal workflow and
  activity, every statement, every outbound call, every gateway and agent call; one
  trace per ticket from first sighting to closure. 10% kept, all errors, all execution.
- Logs in Log Analytics (`AppTraces`), JSON on stdout too, scrubbed.
- Metrics in the Azure Monitor workspace; dashboards in Grafana's "CloudOps dev" folder.
- No tenant anywhere in any of them. To find which customer a trace concerns, follow
  its trace id into the database (`cloudops.llm_call`, `cloudops.agent_run`,
  `cloudops.work_item.trace_parent`, `audit.action_log`) through the console or with
  your own scoped access.
