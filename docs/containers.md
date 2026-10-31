# Containers

Images for the platform's deployables, built by `docker-bake.hcl`: the Python ones
from one Dockerfile (`infra/docker/python.Dockerfile`), the operations console from
`infra/docker/web.Dockerfile`. Decisions: ADR 0007; the console, ADR 0017.

| Image | Runs | Port | Probes |
| --- | --- | --- | --- |
| `api` | `python -m cloudops_api` | 8000 | `/healthz`, `/readyz` (database) |
| `connector-servicenow` | `python -m cloudops_connectors.servicenow worker` | 8080 | `/healthz`, `/readyz` (database, Temporal, bronze) |
| `worker-ai` | `python -m cloudops_agents.worker` | 8080 | `/healthz`, `/readyz` (database, Temporal) |
| `web` | `node server.js` (Next.js standalone, distroless Node) | 3000 | `/healthz` (configuration complete) |

Every image runs as UID 65532 with no shell, and needs no writable path: run it
with a read-only root filesystem, all capabilities dropped and `RuntimeDefault`
seccomp (step 12 sets these on the pods). The one exception is `web`, which writes
Next's cache to `/app/apps/web/.next/cache`: an `emptyDir` in the pod.

## Build and check

```bash
make images        # bake every image, then SBOM + scan each
make image-check   # SBOM + scan the images already built
```

`make images` fails if any image would run as root or has a critical or high
vulnerability not listed in `.grype.yaml`. SBOMs (SPDX and CycloneDX JSON) land in
`build/sbom/`.

Tools: `docker buildx`, `syft`, `grype`. Signing is not local: the CI pipeline signs
(step 13).

## Push to the dev registry

Stacks `04a-registry` and then `04b-rbac` (for your AcrPush) must be applied first.
Push only images that passed the gate, tagged with the commit, never `latest`:

```bash
az acr login -n crcloudopsdevweu
```

```bash
TAG=$(git rev-parse --short HEAD); for i in api connector-servicenow worker-ai migrate web; do docker tag cloudops/$i:dev crcloudopsdevweu.azurecr.io/$i:$TAG && docker push crcloudopsdevweu.azurecr.io/$i:$TAG; done
```

`az acr login` stores a registry token (valid about three hours) in
`~/.docker/config.json`, issued from your Entra sign-in; no registry password exists.

## Bumping a base image

1. Resolve the new digest: `docker buildx imagetools inspect <image>:<tag>`.
2. Replace the `@sha256:` in the Dockerfile's `FROM` line.
3. `make images`. If the gate fails on an exception that no longer matches, the fix
   has shipped: delete that rule from `.grype.yaml`.

## Adding a deployable

Add a final stage (`FROM runtime AS <name>`, with its `ENTRYPOINT`) and a bake target
with its `PACKAGE`. `tools/ci/tests/test_containers.py` checks the two agree.
