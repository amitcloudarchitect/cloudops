# One Dockerfile for every Python deployable; docker-bake.hcl picks the final
# stage (target) and the workspace package (PACKAGE; "all" for the whole root
# environment, which the migrations use). Context: the repository root.
#
# LLD, Compute (pod controls): the runtime is distroless (no shell, no package
# manager), runs as UID 65532, and needs no writable path, so the pod can run with
# a read-only root filesystem and every capability dropped. Base images are pinned
# by digest; bump them deliberately (docs/containers.md).

FROM ghcr.io/astral-sh/uv:0.11.21@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946 AS uv

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:3ee00291cf386ec52a552e6a858d555debd74776eb4bfa53fa3df2cd6677515e AS distroless

# --- build: resolve the locked dependencies into /app/.venv ----------------------------
FROM python:3.12-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
ARG PACKAGE
RUN test -n "$PACKAGE" \
 && if [ "$PACKAGE" = all ]; then uv sync --frozen --no-dev --no-editable; \
    else uv sync --frozen --no-dev --no-editable --package "$PACKAGE"; fi

# The interpreter without what a runtime never uses: pip (a package manager), the
# test suite, IDLE, tkinter, ensurepip, and the extension modules for terminals and
# dbm files, whose libraries (ncurses, readline, gdbm, Berkeley DB) would otherwise
# be copied in. _uuid goes too: the uuid module falls back to pure Python, and
# libuuid (util-linux) carries open high CVEs Debian will not fix.
RUN cd /usr/local/lib/python3.12 \
 && rm -rf site-packages/pip* test idlelib tkinter turtledemo ensurepip lib2to3 curses dbm \
 && rm -f lib-dynload/readline.* lib-dynload/_curses* lib-dynload/_dbm.* lib-dynload/_gdbm.* \
          lib-dynload/_uuid.*

# Shared libraries the interpreter and the wheels link against that distroless lacks,
# copied with their dpkg status entries so SBOM and vulnerability scans still see them.
COPY --from=distroless /var/lib/dpkg/status.d /distroless-status
RUN <<'COLLECT'
set -eu
mkdir -p /rootfs/var/lib/dpkg/status.d
find /usr/local/bin/python3.12 /usr/local/lib /app/.venv -type f \( -name '*.so*' -o -name python3.12 \) \
  -exec ldd {} + 2>/dev/null | awk '$2 == "=>" && $3 ~ /^\// {print $3}' | sort -u > /tmp/libs
owner() {  # the package owning a library path, ignoring dpkg's diversion lines
  for f in "$1" "/usr$1" "$(readlink -f "$1")"; do
    p=$(dpkg-query -S "$f" 2>/dev/null | grep -E '^[a-z0-9.+-]+(:[a-z0-9]+)?: /' | head -n1 | cut -d: -f1)
    [ -n "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}
while read -r lib; do
  case "$lib" in /usr/local/*|/app/*) continue ;; esac
  pkg=$(owner "$lib") && [ -n "$pkg" ] || { echo "no package owns $lib" >&2; exit 1; }
  [ -e "/distroless-status/$pkg" ] && continue
  [ -e "/rootfs/var/lib/dpkg/status.d/$pkg" ] && continue
  dpkg-query -s "$pkg" > "/rootfs/var/lib/dpkg/status.d/$pkg"
  dpkg-query -L "$pkg" | while read -r f; do
    case "$f" in *.so|*.so.*) [ -e "$f" ] && cp -a --parents "$f" /rootfs ;; esac
  done
done < /tmp/libs
echo "added from Debian:" $(ls /rootfs/var/lib/dpkg/status.d)
COLLECT

# --- runtime ---------------------------------------------------------------------------
FROM distroless AS runtime
COPY --from=build /usr/local/lib /usr/local/lib
COPY --from=build /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=build /rootfs/ /
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:/usr/local/bin \
    LD_LIBRARY_PATH=/usr/local/lib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 65532:65532

# --- one final stage per deployable ----------------------------------------------------
FROM runtime AS api
EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/python", "-m", "cloudops_api"]

FROM runtime AS connector-servicenow
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/python", "-m", "cloudops_connectors.servicenow", "worker"]

FROM runtime AS worker-ai
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/python", "-m", "cloudops_agents.worker"]

# Database migrations (Argo CD PreSync hook, ADR 0008). Alembic as the migration
# role; the DSN file comes from a ConfigMap and, in Azure, the password is an
# Entra token (CLOUDOPS_MIGRATION_AUTH=entra).
FROM runtime AS migrate
COPY --from=build /src/alembic.ini /app/alembic.ini
COPY --from=build /src/db/migrations /app/db/migrations
ENTRYPOINT ["/app/.venv/bin/alembic", "-c", "/app/alembic.ini", "upgrade", "head"]
