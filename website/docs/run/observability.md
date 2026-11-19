# Health and observability

## Probes

- `/live` returns `204` without accessing ClickHouse or RabbitMQ. Use it for
  container liveness.
- `/` checks the active ingest backend. Use it for load-balancer readiness;
  an unhealthy backend returns `503`.
- `evnt queue healthcheck` checks the worker's liveness file. It does not prove
  that the queue is empty or that ClickHouse is receiving rows.

Readiness checks can be cached with
`EVNT_PERFORMANCE__HEALTHCHECK_CACHE_TTL_SECONDS`. In RabbitMQ mode, API readiness
checks RabbitMQ; monitor worker delivery and queue depth separately.

## Prometheus

```bash
EVNT_PROMETHEUS__ENABLED=true
EVNT_PROMETHEUS__METRICS_PATH=/metrics
```

The default compose stack uses a shared multiprocess directory for API metrics.
Clear that directory before starting a new group of Uvicorn workers. Scrape the
configured path on the same collector port.

`evnt_events_dropped_total{reason}` counts tracker events that could not be
stored while the rest of their request was. `reason` is `not_an_object` (a batch
element that is not a JSON object), `invalid_event_type` (missing or unknown
`e`), `unrepairable` (validation kept failing after the bad fields were
removed) or `processing_error` (even the fallback row could not be built).

`evnt_geo_header_values_rejected_total{field}` counts Cloudflare visitor
location header values left out of `extra.collector.geo` because they failed
validation (`field` is the stored key, such as `city` or `latitude`). Empty
values, which Cloudflare sends for fields it has no data for, are not counted. It only
moves when `EVNT_COMMON__SNOWPLOW__CLOUDFLARE_GEO_HEADERS` is on; a steady rise
for one field points at a proxy sending values in an unexpected format.

## Malformed tracker input in the log

Each dropped event is logged as `Dropped tracker event` with its `reason`, and
an event whose parsing raised is logged as `Tracker event failed to parse` with
the exception type and traceback. Both are rate-limited per reason or exception
type: the first occurrence is logged, then at most one line a minute carrying
`suppressed`, the number of occurrences held back. Events stored with problems
are not logged one by one; query `extra.validation` instead. Context entities
without a column mapping are not logged either: they are kept in
`extra.contexts`.

## Optional integrations

Build with the corresponding optional dependency before enabling an integration:

```bash
docker build --build-arg EXTRAS='sentry apm crypto' -t evnt:local .
```

- `EVNT_SENTRY__ENABLED=true` enables Sentry; configure `EVNT_SENTRY__DSN`.
- `EVNT_ELASTIC_APM__ENABLED=true` enables Elastic APM; configure its server
  and service settings with the `EVNT_ELASTIC_APM__` prefix.

Inspect defaults with `evnt settings`. Missing optional dependencies cause an
explicit startup error when their integration is enabled.
