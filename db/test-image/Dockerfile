# PostgreSQL 16 with the extensions the LLD requires (pgvector, pg_partman), for
# CI, Testcontainers and the local stack (docker-compose.yml) only. Production is
# Azure Database for PostgreSQL Flexible Server, where these are enabled through
# azure.extensions.
FROM pgvector/pgvector:0.8.0-pg16

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-partman \
 && rm -rf /var/lib/apt/lists/*
