#!/usr/bin/env bash
# First-start provisioning for the local PostgreSQL (docker-entrypoint-initdb.d).
# Runs once, as the superuser, when the data volume is empty.
#
# Mirrors what infrastructure does in Azure, so the migration and the application
# meet the same roles they will meet there:
#
#   cloudops_migrator        owns the database and the schema; runs alembic
#   cloudops_app             group roles the migration grants privileges to, created by
#   cloudops_ingest            db/bootstrap/roles.sql, exactly as in Azure
#   cloudops_app_local       login, member of cloudops_app    — what services use
#   cloudops_ingest_local    login, member of cloudops_ingest — what connectors and the seed use
#   cloudops_resolver        own the access-control functions (migration 0004), also
#   cloudops_access_admin      from roles.sql; NOLOGIN, reachable only through them
#
# No login here is superuser, BYPASSRLS, or a table owner except the migrator;
# packages/tenancy refuses to run as anything else.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v migrator_pw="$CLOUDOPS_MIGRATOR_PASSWORD" <<'SQL'
CREATE ROLE cloudops_migrator LOGIN NOSUPERUSER NOBYPASSRLS CREATEROLE PASSWORD :'migrator_pw';
SQL

# The same server bootstrap Azure runs (db/bootstrap/roles.sql): group roles and
# the access-function owners. Run twice to prove it is idempotent.
for _ in 1 2; do
    psql --quiet --username "$POSTGRES_USER" --dbname postgres \
        -v migrator=cloudops_migrator -f /cloudops/bootstrap/roles.sql
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v app_pw="$CLOUDOPS_APP_PASSWORD" \
    -v ingest_pw="$CLOUDOPS_INGEST_PASSWORD" <<'SQL'
CREATE ROLE cloudops_app_local    LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_pw'
    IN ROLE cloudops_app;
CREATE ROLE cloudops_ingest_local LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'ingest_pw'
    IN ROLE cloudops_ingest;

CREATE DATABASE cloudops OWNER cloudops_migrator;
REVOKE ALL ON DATABASE cloudops FROM PUBLIC;
GRANT CONNECT ON DATABASE cloudops TO cloudops_app, cloudops_ingest;

\connect cloudops

-- Extensions are an administrator's job (azure.extensions in Flexible Server).
CREATE EXTENSION vector;
CREATE EXTENSION pg_stat_statements;
CREATE SCHEMA partman;
CREATE EXTENSION pg_partman SCHEMA partman;
GRANT ALL ON SCHEMA partman TO cloudops_migrator;
GRANT ALL ON ALL TABLES IN SCHEMA partman TO cloudops_migrator;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO cloudops_migrator;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO cloudops_migrator;
SQL
