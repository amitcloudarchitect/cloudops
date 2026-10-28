#!/usr/bin/env bash
# First-start provisioning for the local ClickHouse (docker-entrypoint-initdb.d).
#
# One user per service, as the LLD requires (Telemetry store); the image's
# entrypoint has already replaced `default` with cloudops_admin. No tables yet:
# the telemetry schema arrives with the telemetry step, once LLD open item L1
# (ClickHouse or Azure Data Explorer) is decided.
set -euo pipefail

clickhouse client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
    --param_pw="$CLOUDOPS_TELEMETRY_PASSWORD" --multiquery <<'SQL'
CREATE USER IF NOT EXISTS cloudops_telemetry IDENTIFIED WITH sha256_password BY {pw:String};
GRANT SELECT, INSERT ON telemetry.* TO cloudops_telemetry;
SQL
