# ADR 0028: SharePoint, Qualys and CoreStack, read for real in dev

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar
- **Implements:** the live paths of three step 15 connectors that ran on recorded
  fixtures only: `connectors/src/cloudops_connectors/{sharepoint,qualys,corestack}/`,
  `packages/config` (`SHAREPOINT`, `QUALYS`, `CORESTACK`), migration
  `0028_real_connectors`, `cloudops_devdata.simulation.qualys_live`, the GitOps bases
  `infra/gitops/base/connector-{sharepoint,qualys,corestack}` and their dev overlay,
  stacks `04-data`, `04b-rbac` and `05-aks` (`connectors.tf`, the federation), and
  `docs/real-connectors.md`.
- **Amends:** ADR 0016 (the Qualys customer is a plain asset tag, not `Customer:`; the
  CoreStack API shape it assumed is replaced by the real one); the LLD's
  `app-cloudops-sharepoint` ("Sites.Selected only, per site"): per folder, with
  ListItems.SelectedOperations.Selected.

## Context

The SharePoint, Qualys and CoreStack connectors were built in step 15 against an
assumed API shape and recorded fixtures, and refused to start without `--fixtures`
(CLAUDE.md rule 2). Each is now connected in dev, read-only, as far as each source
allows today. Several things were not specified and were asked (2026-11-01):

| # | Question | Decision (user) |
| --- | --- | --- |
| 1 | Identifying values (URLs, client ids, usernames, folder ids): Key Vault or configuration? | **The dev overlay**, as ServiceNow's. Key Vault holds secrets only. |
| 2 | The documents live in Getronics' corporate SharePoint, where no app can be registered yet. | **Mirror them into the dev directory** (gtnmpn): site `Getronics_PubC`, folder `Customer_Documents`. The corporate site is for production, once an app can be registered there. A user account in gtnmpn was rejected: it would be a stored credential. |
| 3 | A site per customer, or a folder? | **A folder per customer**, named as the customer, under one root folder. |
| 4 | Grant the site (Sites.Selected) or each folder? | **Each folder** (ListItems.SelectedOperations.Selected, `read`), so the app sees nothing it was not given. |
| 5 | CoreStack credentials for dev? | **The user's own access key pair**, a time-limited exception (decision 7). |
| 6 | Which CoreStack recommendations? | **Cost** into `cost_finding`; **Security and Compliance** into a new `security_finding`; the rest (Operation) stays in bronze. |
| 7 | How is a Qualys host's customer known? | **Its asset tags**, through the customer mapping. The real subscription tags each customer's hosts with the customer's name as a plain tag, beside `Cloud Agent` and `Default`. |
| 8 | Qualys in dev before there is a Qualys API user? | **Show the simulated Azure Sponsorship vulnerabilities in dev too** (decision 8). |

## Decisions

1. **SharePoint: a folder per customer, read whole.** Each customer folder is granted
   to `app-cloudops-sharepoint` on its own; the folders to read are configuration
   (`CLOUDOPS_SHAREPOINT_FOLDERS`, `<drive id>/<item id>`). The folder's name is the
   customer alias (`sharepoint`), resolved like any source's company name: an unmapped
   name is quarantined for the customer mapping and nothing below it is listed or
   downloaded. Graph's delta works only at a drive's root, which the app cannot read,
   so each run reads the folder whole (the folder, then `/children` of every folder
   below it) into one bronze page of the exact responses, and a document missing from
   it is deleted with its chunks. A snapshot is all or nothing (5,000 items, 12 levels).
   A file's content is downloaded only when that version is not already in bronze. A
   403 or 404 on the folder revokes it: its chunks deleted, its documents marked
   deleted, its row `revoked`. Its tenant is kept as its cursor, so the revocation needs
   nothing from Graph. A folder filed under one tenant whose name later resolves to
   another fails its page (the row is invisible under the new scope): a person moves
   it. The new table `sharepoint_folder` is tenant data under FORCE RLS, tenant-wide
   like a site; `document.folder_id` records the folder. The site sync (Sites.Selected)
   is unchanged.

2. **SharePoint authenticates with no secret.** The worker's projected service account
   token is exchanged for the app registration's token through a federated credential
   on the app (`workload_credential(identity, client_id=...)`). The app was created by
   hand in the dev directory, so its federated credential is too
   (`docs/real-connectors.md`): stack 05 would need the `azuread` provider and Graph
   application permissions for CI, which the platform does not hold. The pod's managed
   identity (`id-cloudops-ingest`) still writes the bronze container.

3. **Qualys: the customer is an asset tag.** Every tag except `Cloud Agent` and
   `Default` is a candidate customer name; a `Customer:<name>` tag, where present, is
   the only candidate. Each candidate is resolved; one tenant files the host, none
   quarantines each candidate with a count, two reject the host (counted, kept in
   bronze). The live path reads as the API user (`CLOUDOPS_QUALYS_URL`,
   `CLOUDOPS_QUALYS_USERNAME`, the password `qualys-api-password`, read for each
   request) with HTTP Basic over https to that host only.

4. **CoreStack: the real API.** `POST /v1/auth/tokens` with the access key pair returns
   a token (`X-Auth-Token`), kept until a minute before it expires and fetched again
   once on a 401; both keys are read from the store for each token request. Recommendations are
   `GET /recommendations/{tenant}/service_account/list` and
   `GET /recommendations/{tenant}/get/{id}`, numbered pages. The client can send those
   three requests and nothing else (`client.ALLOWED`, checked before anything leaves).
   There is no change feed and no money: each run reads a CoreStack tenant whole into
   one bronze page, one finding per recommendation per resource, and an open finding
   the snapshot no longer holds is resolved (`source_scope` on both finding tables).
   The account is the subscription in the resource's id (CoreStack's
   `cloud_account_id` is its own). Savings stay NULL.

5. **`security_finding`**: CoreStack's Security and Compliance recommendations, shaped
   like `cost_finding` without money, with severity (high, medium, low), policy and
   detector. Tenant data under FORCE RLS, restricted by account like cost. Staff only:
   the customer role has no grant.

6. **The shared subscription** (ADR 0027): a finding on the platform's own subscription,
   which `azsponsor` holds too, links under the customer that holds the account, even
   though the subscription's alias names the internal tenant. An account the tenant
   does not hold, whose alias names another tenant, rejects the finding.

7. **Dev exception: CoreStack with a person's own keys.** Until a dedicated read-only
   CoreStack user exists, dev reads with the user's own access key pair, both halves
   in Key Vault (`corestack-access-key`, `corestack-secret-key`). The key id is not a
   secret, but it names a person's key and the secret scanner treats it as one, so it
   stays out of the public repository rather than be allowlisted. It ends before
   UAT: a CoreStack user with a read-only role, its own key pair, and the old pair
   revoked. The client's allowlist bounds what the connector sends, whatever the key
   could do.

8. **Dev exception: simulated Qualys.** `CLOUDOPS_QUALYS_SOURCE=simulation` serves the
   simulated Azure Sponsorship estate (ADR 0025) through the real connector code. It is
   refused unless `CLOUDOPS_DEPLOYMENT` is `dev`; it runs from
   `cloudops_devdata.simulation.qualys_live` (the `migrate` image, which carries the
   simulation); its instance is `qualys.simulation.invalid` and every row says
   `"simulated": true`. The estate is the VMs the platform holds for the tenant the
   `Azure Sponsorship` alias resolves to, less the platform's own (`*-cloudops-*`),
   with a fixed anchor so findings do not move daily. It is replaced by `live` when
   dev has a Qualys API user, and the simulated rows (`raw_extra.simulated`) are then
   deleted (`docs/real-connectors.md`).

9. **One worker per source**, as `connector-servicenow`: KEDA on the task queue, a
   schedule job, a hashed ConfigMap, a SecretProviderClass, and its own bronze
   container written by the ingest identity (stacks 04-data and 04b-rbac).

## Consequences

- Connector health shows `sharepoint/folder_item` (new schedule row), `qualys` and
  `corestack` from their first runs.
- Folder names with underscores (`Azure_Sponsorship_Account`) do not normalise to the
  spaced name: the company normaliser splits on whitespace only. Such a folder is
  quarantined until mapped on the customer mapping screen, which is the intended path.
- CoreStack's list paging parameters are not documented; the client sends `page` and
  `limit` and stops at a page that adds nothing new, so a server that ignores them is
  read once.
- Production needs, before UAT: an app registration in the corporate directory with
  per-folder grants; a CoreStack read-only user; a Qualys API user; the simulation off.
