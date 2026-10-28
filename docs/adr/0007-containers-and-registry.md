# ADR 0007: Container images, the dev registry, and where signing happens

- **Status:** Accepted, 2026-10-28
- **Decided by:** Amit Kumar (step 11)
- **Implements:** LLD, Compute (pod controls, images only from the platform
  registry); LLD, CI/CD (signed image build, container scan). Code in
  `infra/docker/python.Dockerfile`, `docker-bake.hcl`, `scripts/image-check.sh`,
  `.grype.yaml`, `packages/config/src/cloudops_config/probes.py`, stack
  `04a-registry`.

## Decisions

1. **Images for what runs today, and an AI worker.**
   - `api`, `connector-servicenow` (the ServiceNow Temporal worker) and `worker-ai`
     (`cloudops_agents.worker`: the triage workflow, stages 1 to 3).
   - `web` waits for step 14 (`apps/web` is empty), the telemetry processor for LLD
     open item L1 (step 15), and the execution worker for the security review.
2. **The AI gateway runs in-process in `worker-ai`, not as its own service.**
   - It is a library today, and every control it enforces (quotas, redaction, the
     breaker, the tool registry's permission and scope checks) runs inside the
     caller's database transaction under RLS. A network hop would add a service
     to authenticate without adding a control.
   - The LLD puts the gateway and Temporal workers on the same pool (`npai`), so
     placement is unchanged. Revisit when a second consumer (the assistant, step
     14) needs the gateway outside a Temporal worker.
   - `worker-ai` cannot start in Azure until the Azure OpenAI tiers exist (LLD L12):
     the configuration layer refuses the LLM component there.
3. **One Dockerfile, distroless runtime.**
   - Build on `python:3.12-slim-trixie`, run on `gcr.io/distroless/cc-debian13:nonroot`
     (no shell, no package manager), UID 65532, both pinned by digest.
   - The interpreter and the venv are copied in. The few Debian libraries they link
     against are copied **with their dpkg status entries**, so SBOMs and scans see
     them. Unused extension modules (readline, curses, dbm, `_uuid`) are removed so
     their libraries never enter the image.
   - Nothing writes to the filesystem at runtime: bytecode is compiled at build.
4. **Every service has a cheap liveness probe and a real readiness probe.**
   - The API serves `/healthz` and `/readyz` itself. Workers use `cloudops_config.probes`
     on port 8080.
   - Readiness runs each dependency check with a 2-second timeout: the database
     (`Database.ping`, under a scope that sees no tenant rows), Temporal, and for the
     ServiceNow worker the bronze container. It names failed checks and never
     includes an exception's text.
5. **The vulnerability gate fails on critical or high, with reviewed exceptions.**
   - Grype scans the Syft SBOM. `.grype.yaml` lists the exceptions: each one names a
     CVE and an anchored package of the distroless base, is limited to Debian's
     "won't fix" or "not fixed" state, and carries a reason and a review date. A
     test rejects any rule broader than that.
   - When Debian ships a fix, the rule stops matching and the gate fails until the
     base digest is bumped.
6. **Signing happens in CI (step 13), not locally.**
   - Keyless Sigstore would publish every image name to a public log; a key on a
     laptop would be a long-lived secret. The pipeline signs with its own federated
     identity, and the cluster verifies.
   - Until then, locally built and pushed images are unsigned. Nothing that verifies
     signatures (the execution namespace's policy) exists yet.
7. **The dev registry is Basic with public network access** (`04a-registry`), for
   cost. Admin user and anonymous pull are off, so access is Entra-only (AcrPull for
   the kubelet identity, AcrPush for named people in dev, both in `04b-rbac`). UAT and
   production use Premium with a private endpoint, as the LLD says.

## Consequences

- Base images move only by a deliberate digest bump (`docs/containers.md`), which
  reruns the gate.
- Images pushed before step 13 carry no signature or attached SBOM; treat them as
  dev-only. Step 13 replaces local pushes with the pipeline.
