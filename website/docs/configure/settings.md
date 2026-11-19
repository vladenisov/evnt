# Settings

Settings are loaded from a single Pydantic `BaseSettings` model. Use the `EVNT_` prefix and `__` for nested keys. List/dict values are parsed as JSON:

```bash
EVNT_COMMON__DEMO=true                          # enable the /demo/ SPA at runtime
EVNT_CLICKHOUSE__CONNECTION__HOST=clickhouse    # CH host
EVNT_INGEST__MODE=rabbitmq                      # direct (default) | rabbitmq
EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["https://example.com"]'
```

Inspect the full config tree (with defaults) any time:

```bash
evnt settings          # from backend/: uv run evnt settings
```

A starter [`.env.example`](https://github.com/vladenisov/evnt/blob/main/.env.example) lists the most common runtime variables.

## Client network metadata

| Setting | Default | Meaning |
| --- | --- | --- |
| `EVNT_COMMON__SNOWPLOW__USER_IP_HEADER` | `X-Forwarded-For` | Trusted proxy header containing the original client IP, or a comma-separated list checked in order (the first parseable value wins). Behind Cloudflare, use `CF-Connecting-IP,X-Forwarded-For`: `X-Forwarded-For` covers requests reaching the collector through another proxy. |
| `EVNT_COMMON__SNOWPLOW__USER_COUNTRY_HEADER` | unset | Optional trusted proxy country header, for example `CF-IPCountry`. Omit it to disable country-header ingestion. |

Both headers require `EVNT_SECURITY__TRUST_PROXY_HEADERS=true`. With proxy
trust disabled, the direct peer provides the client IP and country headers are
ignored. See [proxy trust](security.md).

The existing `user_ip` column remains IPv4. The full original address, including
native IPv6, is also saved as `extra.collector.client_ip`; the configured country
header is saved as `extra.collector.ip_country`. The collector owns this JSON
namespace and replaces any payload-supplied values. Missing values use empty
strings. Country codes are uppercased and must contain two ASCII letters or `T1`;
Cloudflare's `XX` (unknown) and `T1` (Tor) are retained.

These fields use the existing `extra` JSON column and require no schema migration.
Cloudflare's `CF-Pseudo-IPv4` is a synthetic address and is not used for client
identity or geolocation. Use Pseudo IPv4 **Off** or **Add Header** so
`CF-Connecting-IP` retains the original address; **Overwrite Headers** replaces
it with a synthetic address. See [Cloudflare's header reference](https://developers.cloudflare.com/fundamentals/reference/http-headers/).

## ClickHouse and RabbitMQ

| Setting | Default |
| --- | --- |
| `EVNT_CLICKHOUSE__CONNECTION__HOST` | `clickhouse` |
| `EVNT_CLICKHOUSE__CONNECTION__PORT` | `8123` |
| `EVNT_CLICKHOUSE__CONNECTION__USERNAME` | `default` |
| `EVNT_CLICKHOUSE__CONNECTION__PASSWORD` | `password` (override in production) |
| `EVNT_CLICKHOUSE__STARTUP_TIMEOUT_SECONDS` | `60` |
| `EVNT_INGEST__MODE` | `direct` (alternative: `rabbitmq`) |
| `EVNT_INGEST__RABBITMQ__HOST` | `rabbitmq` |
| `EVNT_INGEST__RABBITMQ__PORT` | `5672` |
| `EVNT_INGEST__RABBITMQ__QUEUE_NAME` | `evnt.ingest` |
| `EVNT_INGEST__RABBITMQ__BATCH_SIZE` | `500` |
| `EVNT_INGEST__RABBITMQ__INSERT_TIMEOUT_SECONDS` | `60` |
| `EVNT_INGEST__RABBITMQ__PUBLISH_TIMEOUT_SECONDS` | `5` |
| `EVNT_INGEST__RABBITMQ__MAX_INFLIGHT_PUBLISHES` | `200` unconfirmed messages per process (up to ~180 KB each when merged) |
| `EVNT_INGEST__RABBITMQ__COALESCE_WINDOW_MS` | `5` (`0` publishes one message per request) |
| `EVNT_INGEST__RABBITMQ__COALESCE_MAX_ROWS` | `100` |
| `EVNT_INGEST__RABBITMQ__MAX_INSERT_ATTEMPTS` | `10` attempts before a message that keeps failing while other inserts succeed is moved to the failed queue (`0` retries forever) |

On startup the app (and, in `rabbitmq` mode, the worker) retries the ClickHouse connection until `startup_timeout_seconds` elapses.

In `rabbitmq` mode a request returns only after the broker confirms the message
holding its events. Rows from requests that arrive within
`coalesce_window_ms` of each other share one message (up to
`coalesce_max_rows` rows; a single request with more rows than that is
published as a message of its own, as without merging), because a publish costs more CPU than processing a
typical request: merging cut collector CPU per request by about 90% in our
measurements, for a few milliseconds of added latency. If one row in a merged
message fails ClickHouse validation, the worker inserts the other rows and
moves only the failing ones to the failed queue.

The worker sorts insert failures into three kinds:

- **Bad rows** (the driver rejects a value, a value cannot be converted or
  serialized, or ClickHouse reports a parse, range, enum or constraint error):
  the worker bisects the batch, trying both halves before splitting either,
  and moves only the failing rows to the failed queue.
- **Outages** (timeouts, lost connections, an HTTP error from a proxy, or a
  ClickHouse error that hits every insert, such as `TOO_MANY_PARTS`,
  `MEMORY_LIMIT_EXCEEDED`, a read-only table, Keeper errors, authentication
  failures or a table that no longer matches the collector's columns): the
  messages stay in the queue and are retried with backoff for as long as the
  outage lasts.
- **Anything else**: retried, and after `max_insert_attempts` failed attempts
  the message is moved to the failed queue.

Bad rows are moved to the failed queue at once when another insert of the same
flush succeeded; the ClickHouse driver's own value check (`DataError`) needs no
such proof. Otherwise (a message flushed alone, a message whose every row
fails, or an unexplained failure) the attempt counts toward
`max_insert_attempts`, and on the last attempt the message is moved only if
some insert of the worker, in any table group, succeeded since its previous
failed attempt. A failure that hits every insert (a schema change or a code
bug) therefore holds the queue instead of emptying it into the failed queue,
and outages never count. A table group the worker cannot insert at all, for
example one added by a newer collector version, is moved after
`max_insert_attempts` while other groups insert. The attempt count lives in
each worker's memory and starts over when the worker restarts.

Each failed-queue message keeps the original headers and adds
`x-evnt-source-queue`, `x-evnt-table-group`, `x-evnt-error`,
`x-evnt-error-type`, `x-evnt-failure` (`deterministic` or
`max_insert_attempts`), `x-evnt-error-code` when ClickHouse returned one, and
`x-evnt-insert-attempts` for messages moved after `max_insert_attempts`.

If the broker does not confirm within `publish_timeout_seconds` (for example
while it is blocked by a memory or disk alarm), if `max_inflight_publishes`
unconfirmed messages are already waiting, or if the connection is lost or the
queue is missing, the collector answers `503` with `Retry-After` and trackers
resend the batch. A message cut off by the timeout may still be stored once the
broker recovers, so a retry can duplicate events; deduplicate on `eid`
downstream if that matters.

The readiness check (`GET /`) runs on a connection of its own and reports
whether the ingest queues exist. It stays healthy during a broker alarm, when
publishes answer `503`: the broker is shared by every collector instance, so
taking instances out of rotation would not help.

The worker prefetches `prefetch_count` messages (default `500`). A merged
message holds up to `coalesce_max_rows` rows (a single larger request is
never merged), so lower `prefetch_count` if
worker memory matters more than throughput.

## Secrets

`EVNT_CLICKHOUSE__CONNECTION__PASSWORD` and `EVNT_INGEST__RABBITMQ__PASSWORD` are stored as Pydantic `SecretStr`: they are still configured the same way via environment variables, but their values are redacted from config dumps (`evnt settings`) and logs.
