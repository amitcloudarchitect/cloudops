# The operations console (apps/web; ADR 0014, ADR 0017): Next.js `standalone` on
# distroless Node. Context: the repository root (the npm workspace).
#
# One image for every environment. Only the sign-in mode is built in (entra; a local
# build is for a developer's machine and never pushed). The client id, directory,
# API scope and API base are read from the pod's environment at request time.
#
# LLD, Compute (pod controls): no shell, no package manager, UID 65532, and the only
# writable path is Next's cache directory, an emptyDir in the pod. Base images are
# pinned by digest; bump them deliberately (docs/containers.md).

FROM gcr.io/distroless/nodejs24-debian13:nonroot@sha256:a6c34457f6da4295807e051411c7783739c65b997a324fed14e4fa292615ae91 AS distroless

# --- build ------------------------------------------------------------------------------
FROM node:24.22.2-trixie-slim@sha256:54f84b4cf8432b21aaffd5d3ce8d41e12cea0d10d33c09d2f3c9879d49d6b446 AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /src
COPY package.json package-lock.json ./
COPY packages/contracts/package.json packages/contracts/
COPY apps/web/package.json apps/web/
# No install scripts: nothing a dependency ships runs on the builder.
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY packages/contracts packages/contracts
COPY apps/web apps/web
ARG NEXT_PUBLIC_AUTH_MODE
RUN test "$NEXT_PUBLIC_AUTH_MODE" = entra \
 && npm run build -w @cloudops/web \
 && cp -r apps/web/.next/static apps/web/.next/standalone/apps/web/.next/static \
 && mkdir -p apps/web/.next/standalone/apps/web/.next/cache

# --- runtime ----------------------------------------------------------------------------
FROM distroless AS runtime
COPY --from=build --chown=0:0 /src/apps/web/.next/standalone /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app/apps/web
USER 65532:65532

FROM runtime AS web
EXPOSE 3000
ENTRYPOINT ["/nodejs/bin/node", "server.js"]
