# RabbitMQ worker

In `rabbitmq` mode a separate worker drains the queue and batch-inserts into ClickHouse (`evnt queue worker`). It shuts down cleanly on `SIGTERM` (final flush + close), publishes to the failed queue with publisher confirms to avoid silent loss, times out and requeues a stuck ClickHouse insert, and backs off with capped exponential delay on downstream outages.

The worker writes a liveness file that a dedicated healthcheck reads:

```bash
evnt queue healthcheck
```

This is wired as the worker container `HEALTHCHECK` in [`compose.yml`](https://github.com/vladenisov/evnt/blob/main/compose.yml); it reports unhealthy if the worker stops refreshing liveness. The staleness threshold stays above the worker's max backoff so a sustained backend outage is not misread as a dead worker.

The `rabbitmq` compose profile adds the broker and worker. It does not change
the API ingest mode: set `EVNT_INGEST__MODE=rabbitmq` explicitly.

```bash
EVNT_INGEST__MODE=rabbitmq docker compose --profile rabbitmq up -d
```

## Delivery semantics

The API acknowledges publication to RabbitMQ; the worker performs the
ClickHouse insert and then acknowledges the consumed messages. Inserts are
retried on transient failures. A failed batch can be redelivered, so do not
assume exactly-once delivery. Event IDs let downstream queries identify retries.

The worker requires both RabbitMQ and ClickHouse connection settings. The API
in this mode needs RabbitMQ for ingestion, even while ClickHouse is unavailable.
