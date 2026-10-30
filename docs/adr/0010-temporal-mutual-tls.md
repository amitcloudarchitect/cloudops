# ADR 0010: Mutual TLS to Temporal, from an in-cluster CA

- **Status:** Accepted, 2026-10-30
- **Decided by:** Amit Kumar (cert-manager over Key Vault certificates)
- **Implements:** HLD "mutual TLS between services" for Temporal; closes the
  ADR 0008 decision 4 open item on mTLS (authorization stays open). Code in
  `cloudops_config` (`temporal_tls.py`, `temporal.py`, the `TEMPORAL` component), the
  two workers, `infra/gitops` (`applications/dev/{cert-manager,temporal,workers}.yaml`,
  `base/internal-ca`, `base/temporal-secrets/certificates.yaml`, the worker bases,
  `overlays/dev/workers`), and `tools/ci/tests/test_gitops.py`.

## Decisions

1. **cert-manager issues every certificate, from a private CA in the cluster.**
   - cert-manager is an Argo CD Application (chart pinned, `v1.21.3`), synced
     first (wave -3).
   - A self-signed bootstrap issuer signs one CA certificate, `cloudops-internal-ca`,
     in the `cert-manager` namespace. The ClusterIssuer `cloudops-internal-ca` issues
     from it.
   - The CA keeps its key on renewal (`rotationPolicy: Never`), so a renewed CA still
     verifies every certificate signed before, and renewal needs no rollout. The CA
     Certificate is never pruned or deleted by a sync: losing its Secret breaks every
     certificate at once.
   - Chosen over Key Vault certificates mounted by the CSI driver. Those would need
     manual issuance and distribution, and a Key Vault role per workload.
2. **Every leaf certificate is short-lived and gets a new key every time.**
   - ECDSA P-256, PKCS#8, 90 days, renewed 30 days before expiry,
     `rotationPolicy: Always`.
   - Client certificates carry `client auth` only and name their workload: a common
     name, and a `spiffe://cloudops.internal/ns/<namespace>/sa/<account>` URI.
3. **Temporal requires client certificates on every connection.**
   - The chart's own `server.tls` mounts two Secrets:
     - `temporal-frontend` serves workers, KEDA and the Web UI;
     - `temporal-internode` covers the services among themselves, and the system
       worker calling the frontend.
   - Both set `requireClientAuth`.
   - The server re-reads renewed files every hour (`refreshInterval`).
   - The Web UI is a client like any other, with host verification on.
4. **Workers present their certificate, and nothing falls back to plaintext.**
   - The Azure profile's `TEMPORAL` component needs `CLOUDOPS_TEMPORAL_TLS_DIR`, and
     the three files in it, at start-up. It also needs an address with a DNS name the
     server's certificate can prove. Without them it refuses to start; there is no
     plaintext path.
   - Only the local profile is plaintext, to the compose container.
   - `cloudops_config.temporal.connect` builds the TLS config. The private key is held
     in memory only and never appears in a repr or an error.
5. **A renewed client certificate restarts the worker, gracefully.**
   - The Python SDK cannot swap TLS material on a live client.
   - `run_worker` checks the mounted files every five minutes. When they change, it
     shuts the worker down (in-flight activities finish) and the process exits;
     Kubernetes starts the container again with the new certificate.
   - Renewal is 30 days early, so a delayed check is harmless.
   - The mount is a whole directory, never `subPath`, so renewed files appear in place.
6. **KEDA reads queue depth over mutual TLS with the worker's own certificate**
   (`TriggerAuthentication`, `tlsServerName` set). Never `unsafeSsl`.
7. **Kubernetes Secrets are mounted only for cert-manager certificates.**
   - Platform secrets still come from Key Vault through the CSI driver.
   - A certificate's key is generated in the cluster and exists nowhere else, so a
     Secret volume is the only way to mount it.
   - The GitOps test allows a Secret volume only when a Certificate in the same tree
     issues that exact Secret.
8. **Workers are their own Argo CD Application, synced after Temporal** (`workers`,
   wave 2).
   - A worker is not ready until Temporal answers. Inside `platform` (wave 0), it
     would hold Temporal (wave 1) back forever on a fresh cluster.
   - The schedule job (`schedule`, a Sync hook) creates each entity's 5-minute
     schedule, or updates an existing one, so every sync is safe to repeat.

## Consequences

- `connector-servicenow` runs in dev. `worker-ai` has its certificate and joins when
  the Azure OpenAI tiers exist (LLD L12).
- **Any certificate from the CA can do anything in Temporal.** mTLS proves who is
  calling, but nothing yet limits what a caller may do. Before production:
  - add a Temporal authorizer and claim mapper keyed on the certificate's URI, so
    `connector-servicenow` can reach only its own namespace and task queue;
  - give each workload its own Temporal namespace.
- **Who can mint a certificate:** anyone allowed to create a `Certificate` in any
  namespace can get one from the ClusterIssuer.
  - Today that is Argo CD and cluster administrators: application service accounts
    mount no API token.
  - Before production, add cert-manager's approver-policy, so only the expected
    names are approved, per namespace.
- The CA is self-signed and lives in the cluster. A replaced cluster gets a new CA,
  which is harmless because every certificate is re-issued with it. For production,
  consider a root in Key Vault Premium (HSM) with the cluster CA as an intermediate.
- Rolling the CA key is a planned operation:
  1. issue the new CA;
  2. trust both CAs;
  3. re-issue the leaves;
  4. remove the old CA.
- cert-manager's images still come from `quay.io` (the ADR 0008 open item on
  mirroring third-party images covers them too).
