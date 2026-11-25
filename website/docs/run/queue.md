# RabbitMQ worker

In `rabbitmq` mode a separate worker drains the queue and batch-inserts into ClickHouse (`evnt queue worker`). It shuts down cleanly on `SIGTERM` (final flush + close), publishes to the failed queue with publisher confirms to avoid silent loss, times out and requeues a stuck ClickHouse insert, and backs off with capped exponential delay on downstream outages.

The worker writes a liveness file that a dedicated healthcheck reads:

```bash
evnt queue healthcheck
```

This is wired as the worker container `HEALTHCHECK` in [`compose.yml`](https://github.com/vladenisov/evnt/blob/main/compose.yml); it reports unhealthy if the worker stops refreshing liveness. The staleness threshold stays above the worker's max backoff so a sustained backend outage is not misread as a dead worker. Waiting for messages and backing off count as healthy; a single batch step (decode and ack, flush, nack) that runs longer than `EVNT_INGEST__RABBITMQ__INSERT_TIMEOUT_SECONDS` plus 60 seconds counts as a stall, so the worker stops refreshing liveness, logs `RabbitMQ batch worker loop stalled`, and the healthcheck restarts it.

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

## Failed queue

Messages the worker cannot insert are moved to the failed queue
(`<queue>.failed` unless `EVNT_INGEST__RABBITMQ__FAILED_QUEUE_NAME` is set)
instead of being dropped; [the settings reference](../configure/settings.md)
describes when rows that ClickHouse refuses are moved and the `x-evnt-*`
headers they carry.

A message whose body is not a queued insert at all (broken JSON, or JSON of
the wrong shape, for example something published to the queue by hand) is
moved there too. Its body is republished byte for byte with the original
headers plus `x-evnt-failure: undecodable`, `x-evnt-source-queue`,
`x-evnt-error` and `x-evnt-error-type`; it has no `x-evnt-table-group`. The
worker acks the original only after the broker confirms the copy. If the
failed queue does not take it (broker alarm, publish timeout, or the queue no
longer exists so the broker returns the copy), the message is requeued and the
worker backs off before the next attempt, so it is never discarded.

`x-evnt-error` is cut to 2 KB on every message moved to the failed queue, and
a validation error with many entries is reduced to its count, so a large
payload cannot push the headers past the broker frame size. The body is what
to inspect.

Nothing consumes the failed queue: inspect it, fix the cause and republish or
purge the messages yourself.

The worker requires both RabbitMQ and ClickHouse connection settings. The API
in this mode needs RabbitMQ for ingestion, even while ClickHouse is unavailable.
