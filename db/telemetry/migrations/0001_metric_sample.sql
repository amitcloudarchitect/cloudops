-- The telemetry column store (LLD, Telemetry store). ClickHouse locally and in CI;
-- LLD open item L1 (ClickHouse or Azure Data Explorer) stays open.
--
-- No row-level security exists here: tenant isolation is the query wrapper in
-- packages/tenancy (cloudops_tenancy.telemetry), the only code that reads or
-- writes these tables. Every row carries its tenant and account.
--
-- Full grain 90 days; 5-minute rollup 13 months; hourly 3 years (LLD).
-- Block deduplication: a batch retried with the same deduplication token (the
-- Event Hubs partition and sequence range) is written once, rollups included.

CREATE TABLE IF NOT EXISTS telemetry.metric_sample
(
    tenant_id   UUID,
    account_id  UUID,
    resource_id UUID,
    metric      LowCardinality(String),
    ts          DateTime64(3, 'UTC'),
    value       Float64,
    unit        LowCardinality(String),
    source      LowCardinality(String),
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toDate(ts)
ORDER BY (tenant_id, resource_id, metric, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY
SETTINGS non_replicated_deduplication_window = 10000;

CREATE TABLE IF NOT EXISTS telemetry.metric_5m
(
    tenant_id   UUID,
    account_id  SimpleAggregateFunction(any, UUID),
    resource_id UUID,
    metric      LowCardinality(String),
    bucket      DateTime('UTC'),
    unit        SimpleAggregateFunction(any, String),
    min_state   AggregateFunction(min, Float64),
    max_state   AggregateFunction(max, Float64),
    sum_state   AggregateFunction(sum, Float64),
    count_state AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (tenant_id, resource_id, metric, bucket)
TTL bucket + INTERVAL 13 MONTH
SETTINGS non_replicated_deduplication_window = 10000;

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.metric_5m_mv TO telemetry.metric_5m AS
SELECT tenant_id, any(account_id) AS account_id, resource_id, metric,
       toStartOfFiveMinutes(ts) AS bucket, any(unit) AS unit,
       minState(value) AS min_state, maxState(value) AS max_state,
       sumState(value) AS sum_state, countState() AS count_state
FROM telemetry.metric_sample
GROUP BY tenant_id, resource_id, metric, bucket;

CREATE TABLE IF NOT EXISTS telemetry.metric_1h
(
    tenant_id   UUID,
    account_id  SimpleAggregateFunction(any, UUID),
    resource_id UUID,
    metric      LowCardinality(String),
    bucket      DateTime('UTC'),
    unit        SimpleAggregateFunction(any, String),
    min_state   AggregateFunction(min, Float64),
    max_state   AggregateFunction(max, Float64),
    sum_state   AggregateFunction(sum, Float64),
    count_state AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYear(bucket)
ORDER BY (tenant_id, resource_id, metric, bucket)
TTL bucket + INTERVAL 3 YEAR
SETTINGS non_replicated_deduplication_window = 10000;

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.metric_1h_mv TO telemetry.metric_1h AS
SELECT tenant_id, any(account_id) AS account_id, resource_id, metric,
       toStartOfHour(ts) AS bucket, any(unit) AS unit,
       minState(value) AS min_state, maxState(value) AS max_state,
       sumState(value) AS sum_state, countState() AS count_state
FROM telemetry.metric_sample
GROUP BY tenant_id, resource_id, metric, bucket;
