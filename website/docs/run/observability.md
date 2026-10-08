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
