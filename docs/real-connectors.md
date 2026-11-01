# SharePoint, Qualys and CoreStack, live in dev: turning them on

ADR 0028. Three workers in `platform-ingest`, each reading one source, read-only:

- `connector-sharepoint`: the customer folders granted to `app-cloudops-sharepoint-dev`.
- `connector-qualys`: the simulated Azure Sponsorship estate (dev only), until dev has
  a Qualys API user.
- `connector-corestack`: the Azure Sponsorship CoreStack tenant.

Nothing here is applied by CI. Do the steps in this order. Steps 1 and 2 must be done
before the pull request merges: Argo CD starts the workers when it syncs, and without
these a worker cannot authenticate.

## 1. Terraform (plan, then apply)

| Stack | What it adds | Notes |
| --- | --- | --- |
| `04-data` | Containers `sharepoint`, `qualys` and `corestack` in bronze (`connectors.tf`). | |
| `04b-rbac` | The ingest identity: Storage Blob Data Contributor on each of the three containers (`connectors.tf`). | Role assignments: needs User Access Administrator. |
| `05-aks` | Federated credentials on the ingest identity for `platform-ingest/connector-sharepoint`, `-qualys` and `-corestack`. | |

```bash
cd infra/terraform/stacks/04-data && terraform init && terraform plan
cd infra/terraform/stacks/04b-rbac && terraform init && terraform plan
cd infra/terraform/stacks/05-aks && terraform init && terraform plan
```

No role in a customer environment, and none on a subscription: each source is read
with its own credential. The app Key Vault is already readable by every workload
identity (stack 04b), so the two secrets need no new grant.

## 2. Entra and Key Vault (by hand)

**SharePoint: the app registration's federated credential.** The app
`app-cloudops-sharepoint-dev` (client id `c8487213-84c3-4975-845f-b02c4b832f01`) was
created by hand, so its federated credential is added by hand too. It lets the
`connector-sharepoint` service account exchange its token for the app's: no secret.

```bash
ISSUER="$(az aks show -g rg-cloudops-aks-dev-weu -n aks-cloudops-dev-weu --query oidcIssuerProfile.issuerUrl -o tsv)"
```

```bash
az ad app federated-credential create --id c8487213-84c3-4975-845f-b02c4b832f01 --parameters "{\"name\":\"aks-platform-ingest-connector-sharepoint\",\"issuer\":\"$ISSUER\",\"subject\":\"system:serviceaccount:platform-ingest:connector-sharepoint\",\"audiences\":[\"api://AzureADTokenExchange\"]}"
```

Already done (2026-11-01): the app holds `ListItems.SelectedOperations.Selected`
(application, admin-consented, nothing delegated), and the folder
`Customer_Documents/Azure_Sponsorship_Account` is granted `read`.

**CoreStack:** both halves of the key pair go in `kv-cloudops-app-dev-weu`:
`corestack-secret-key` (done) and `corestack-access-key`, the access key id (not a
secret, but it names a person's key, so it stays out of the repository). The CSI
driver will not mount a missing secret, so the pod waits for both.

```bash
az keyvault secret set --vault-name kv-cloudops-app-dev-weu --name corestack-access-key --value "<access key id>"
```

**Qualys:** nothing while dev runs the simulation.

## 3. Merge, then let Argo CD roll it out

The workers Application syncs the three Deployments, their KEDA ScaledObjects and the
schedule jobs (every six hours, ADR 0037). The images must be a build of this change: the release
pipeline bumps them in the dev overlay after merge (`Dev overlay: images <sha>`).

```bash
kubectl -n platform-ingest get pods -l 'app.kubernetes.io/name in (connector-sharepoint,connector-qualys,connector-corestack)'
```

Run a first sync now rather than wait a day:

```bash
kubectl -n platform-ingest exec deploy/connector-sharepoint -- /app/.venv/bin/python -m cloudops_connectors.sharepoint run
```

```bash
kubectl -n platform-ingest exec deploy/connector-corestack -- /app/.venv/bin/python -m cloudops_connectors.corestack run
```

```bash
kubectl -n platform-ingest exec deploy/connector-qualys -- /app/.venv/bin/python -m cloudops_devdata.simulation.qualys_live run
```

## 4. Map the customers

Each source names its customer its own way, and the first run quarantines a name
that resolves to no tenant. Map them on the console's customer mapping screen:

| Source | Name it sends | Map to |
| --- | --- | --- |
| `sharepoint` | `Azure_Sponsorship_Account` (the folder's name) | Azure Sponsorship |
| `corestack` | `Microsoft_Azure_Sponsorship-2023` (the CoreStack tenant's name) | Azure Sponsorship |
| `qualys` | `Azure Sponsorship` (the asset tag) | Azure Sponsorship, if it did not already resolve |

Approving a mapping queues a sync of that source at once (ADR 0037): the worker
picks it up within a minute, so there is no need to wait six hours or exec a run.
The next run files everything under the tenant. Connector health on the console shows
each run, its counts and the quarantine depth.

## Adding a customer folder

1. Create `Customer_Documents/<Customer name>` in the site's Documents library.
2. Grant it to the app, `read` only (Graph, as a SharePoint administrator):
   `POST /beta/sites/{site id}/lists/{list id}/items/{item id}/permissions` with
   `{"roles":["read"],"grantedTo":{"application":{"id":"c8487213-84c3-4975-845f-b02c4b832f01"}}}`.
3. Add `<drive id>/<item id>` to `CLOUDOPS_SHAREPOINT_FOLDERS` in
   `infra/gitops/overlays/dev/workers/connector-sharepoint.yaml`.
4. Map the folder's name on the customer mapping screen.

To withdraw one, delete its permission: the next run revokes it and removes its chunks.

## Qualys, live

When dev has a Qualys API user (Reader role, API access only):

1. Put its password in Key Vault as `qualys-api-password`.
2. In `connector-qualys.yaml` (dev workers overlay): `CLOUDOPS_QUALYS_SOURCE: live`,
   `CLOUDOPS_QUALYS_URL` (the platform's API server, `https://qualysapi...`),
   `CLOUDOPS_QUALYS_USERNAME`; add `qualys-api-password` to the SecretProviderClass;
   remove the Deployment and Job patches that run the simulation from the `migrate`
   image.
3. Remove the simulated rows:
   `DELETE FROM cloudops.vulnerability WHERE raw_extra->>'simulated' = 'true'`, as the
   migration owner.

## Before UAT

- CoreStack: a dedicated CoreStack user with a read-only role and its own key pair;
  revoke the personal pair (ADR 0028, decision 7).
- SharePoint: an app registration in the corporate directory, with per-folder grants on
  the real site.
- Qualys: live, never the simulation (the configuration refuses it outside dev).
