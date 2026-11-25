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

## Application

| Setting | Default | Meaning |
| --- | --- | --- |
| `EVNT_COMMON__SERVICE_NAME` | `evnt` | Service name reported to Sentry and Elastic APM. |
| `EVNT_COMMON__HOSTNAME` | unset | Public origin of the collector (scheme, host and optional port), used to build the first-party URLs that `/proxy/hash` returns; see the [proxy](proxy.md). Left unset, the origin of each request is used: its `Host` header and its scheme, read from `X-Forwarded-Proto` when proxy trust is on. `http://localhost:8000` is the fallback when the request has no usable `Host` header. Set it when the collector is reached under a different origin than browsers should load scripts from. |
| `EVNT_COMMON__DEMO_DIR` | `../frontend/dist` (`/app/demo` in the image) | Directory of the built demo SPA, served at `/demo/` when `EVNT_COMMON__DEMO=true`. |
| `EVNT_COMMON__SNOWPLOW__IGLU_SCHEMAS_DIR` | `backend/vendor/iglu-central/schemas` in a checkout (`/app/iglu/schemas` in the image) | Iglu Central `schemas/` directory used to validate self-describing JSON. A schema missing from it validates as `skipped`, not failed. |
| `EVNT_COMMON__SNOWPLOW__ENDPOINTS__PROXY_ENDPOINT` | `/proxy` | Path prefix of the analytics-script [proxy](proxy.md) routes. The other paths are in the [HTTP API](../integrate/http-api.md#changing-paths). |
| `EVNT_ENV` | `development` | Not part of the settings model and not shown by `evnt settings`. Read once when the process starts; its only effect is that `production` changes the default of `EVNT_SENTRY__ENVIRONMENT` to `prod`. |

### Entity schemas with their own columns

Each of these settings holds an Iglu `vendor/name` key. A context entity or
self-describing event with that key gets the special handling described in
its row instead of being stored as a generic entity. Change a key only if your
trackers send the same data under another schema name.

| Setting | Default | Meaning |
| --- | --- | --- |
| `EVNT_COMMON__SNOWPLOW__SCHEMAS__USER_DATA` | `dev.snowplow.simple/user_data` | Context entity stored in the user columns. |
| `EVNT_COMMON__SNOWPLOW__SCHEMAS__PAGE_DATA` | `dev.snowplow.simple/page_data` | Context entity stored in the page columns. |
| `EVNT_COMMON__SNOWPLOW__SCHEMAS__SCREEN_DATA` | `dev.snowplow.simple/screen_data` | Context entity stored in the screen columns. |
| `EVNT_COMMON__SNOWPLOW__SCHEMAS__AD_DATA` | `dev.snowplow.simple/ad_data` | Context entity stored under the `ad_data` key of the `unstructured` JSON column; it has no column of its own. |
| `EVNT_COMMON__SNOWPLOW__SCHEMAS__U2S_DATA` | `dev.snowplow.simple/u2s_data` | Self-describing event converted into a structured (`se`) event: its data must hold the structured event fields. |

## Logging and HTTP behaviour

| Setting | Default | Meaning |
| --- | --- | --- |
| `EVNT_LOGGING__LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case. |
| `EVNT_LOGGING__JSON_FORMAT` | `false` | Write log lines as JSON instead of console text. |
| `EVNT_PERFORMANCE__ENABLE_ACCESS_LOG` | `true` | Log one line per request at `INFO`: the line appears only when `EVNT_LOGGING__LEVEL` is `INFO` or `DEBUG`, so nothing is logged at the default `WARNING`. `false` removes the access-log middleware; `EVNT_PERFORMANCE__ACCESS_LOG_EXCLUDED_PATHS` (default `["/live"]`) only skips the listed paths. |
| `EVNT_PERFORMANCE__ENABLE_BROTLI` | `true` | Compress responses with Brotli (gzip for clients that do not accept Brotli). Turn it off when a proxy in front of the collector already compresses. |
| `EVNT_PERFORMANCE__BROTLI_EXCLUDED_PATHS` | `[]` | Regular expressions; a response whose request path matches one anywhere in the path is not compressed. |
| `EVNT_PERFORMANCE__MAX_CONCURRENT_CONNECTIONS` | `100` | Size of the outbound HTTP connection pool of the analytics-script [proxy](proxy.md). Despite the name, it does not limit inbound connections. |

The other `EVNT_PERFORMANCE__` settings are on the [performance](performance.md) page.

## Sentry and Elastic APM

Both integrations need their optional dependency; see
[optional integrations](../run/observability.md#optional-integrations).

| Setting | Default | Meaning |
| --- | --- | --- |
| `EVNT_SENTRY__ENABLED` | `false` | Report errors to Sentry. |
| `EVNT_SENTRY__DSN` | unset | Sentry DSN. |
| `EVNT_SENTRY__TRACES_SAMPLE_RATE` | `0.0` | Share of requests traced, from `0.0` (no performance tracing) to `1.0`. |
| `EVNT_SENTRY__ENVIRONMENT` | `dev` (`prod` when `EVNT_ENV=production`) | Environment name attached to Sentry events. |
| `EVNT_ELASTIC_APM__ENABLED` | `false` | Add the Elastic APM middleware. |
| `EVNT_ELASTIC_APM__SERVER_URL` | unset | URL of the APM server the agent reports to. Left unset, the agent uses its own `ELASTIC_APM_SERVER_URL` variable, or `http://127.0.0.1:8200`. |
| `EVNT_ELASTIC_APM__SERVICE_NAME` | unset | Service name reported to APM. Left unset, `EVNT_COMMON__SERVICE_NAME` is used. |

## Client network metadata

| Setting | Default | Meaning |
| --- | --- | --- |
| `EVNT_COMMON__SNOWPLOW__USER_IP_HEADER` | `X-Forwarded-For` | Trusted proxy header containing the original client IP, or a comma-separated list checked in order (the first parseable value wins). Behind Cloudflare, use `CF-Connecting-IP,X-Forwarded-For`: `X-Forwarded-For` covers requests reaching the collector through another proxy. |
| `EVNT_COMMON__SNOWPLOW__USER_COUNTRY_HEADER` | unset | Optional trusted proxy country header, for example `CF-IPCountry`. Omit it to disable country-header ingestion. |
| `EVNT_COMMON__SNOWPLOW__CLOUDFLARE_GEO_HEADERS` | `false` | Store Cloudflare's visitor location headers in `extra.collector.geo`; see [visitor location](#visitor-location-cloudflare). |
| `EVNT_COMMON__SNOWPLOW__HONOR_SP_ANONYMOUS` | `false` | Honour the `SP-Anonymous` request header; see [server anonymisation](#server-anonymisation-sp-anonymous). By default the header is ignored. |

The IP, country and location headers require
`EVNT_SECURITY__TRUST_PROXY_HEADERS=true`. With proxy trust disabled, the
direct peer provides the client IP and country and location headers are
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

### Visitor location (Cloudflare)

With `EVNT_COMMON__SNOWPLOW__CLOUDFLARE_GEO_HEADERS=true` and proxy trust on,
the headers of Cloudflare's **Add visitor location headers** managed transform
(Rules > Settings > Managed Transforms; see the
[Cloudflare reference](https://developers.cloudflare.com/rules/transform/managed-transforms/reference/))
are stored in `extra.collector.geo`:

| Header | Key | Stored when |
| --- | --- | --- |
| `cf-ipcity` | `city` | printable text, at most 128 characters |
| `cf-region` | `region` | printable text, at most 128 characters |
| `cf-region-code` | `region_code` | 1 to 8 ASCII letters or digits, uppercased (ISO 3166-2 subdivision, e.g. `TX`) |
| `cf-ipcontinent` | `continent` | two ASCII letters, uppercased, or `T1` (Tor) |
| `cf-iplatitude` | `latitude` | decimal degrees from -90 to 90, stored as a number |
| `cf-iplongitude` | `longitude` | decimal degrees from -180 to 180, stored as a number |
| `cf-postal-code` | `postal_code` | up to 16 ASCII letters, digits, spaces and dashes, starting with a letter or digit |
| `cf-metro-code` | `metro_code` | up to 8 digits, as text |
| `cf-timezone` | `timezone` | an IANA zone name such as `America/Chicago` |

Only the values that pass are stored, and `geo` is absent when none does. A
value that fails is left out without affecting the request and counted in
`evnt_geo_header_values_rejected_total{field}`. Cloudflare sends non-ASCII
names (`São Paulo`) as raw UTF-8 bytes, which the collector decodes as UTF-8; a
percent-encoded name is decoded too, and a value that is not valid UTF-8 is
rejected. The country stays in `ip_country`
(`EVNT_COMMON__SNOWPLOW__USER_COUNTRY_HEADER=CF-IPCountry`). These headers are
only as trustworthy as the proxy: enable this only when every request reaches
the collector through Cloudflare or a proxy that strips client-sent `cf-*`
headers.

```sql
SELECT
    extra.collector.geo.city.:String AS city,
    extra.collector.geo.latitude.:Float64 AS latitude,
    extra.collector.geo.longitude.:Float64 AS longitude
FROM evnt.local
WHERE time >= now() - INTERVAL 1 HOUR
LIMIT 10;
```

### Server anonymisation (`SP-Anonymous`)

The Snowplow trackers send `SP-Anonymous: *` when server-side anonymisation is
on (`anonymousTracking: { withServerAnonymisation: true }` in the JavaScript
tracker, `serverAnonymisation` on iOS and Android). The collector ignores the
header by default, as before. With
`EVNT_COMMON__SNOWPLOW__HONOR_SP_ANONYMOUS=true`, a request that carries it,
with any value, on `/tracker`, `/i` or `/e`:

- stores `user_ip` as `0.0.0.0` and no `extra.collector.client_ip`,
  `ip_country` or `geo`; `extra.collector` is `{"anonymous": true}`;
- ignores the `ip` tracker parameter, also for `p=srv` events, and does not
  keep it in `extra.tracker.ip`;
- does not read the `_sp_id` cookie for a missing `duid`. The collector never
  sets cookies.

`ua` and `tnuid` are still used and kept, since the tracker sent them on
purpose. CORS preflights allow the header, as they allow any request header.

## ClickHouse and RabbitMQ

| Setting | Default |
| --- | --- |
| `EVNT_CLICKHOUSE__CONNECTION__INTERFACE` | `http` (alternative: `https`) |
| `EVNT_CLICKHOUSE__CONNECTION__HOST` | `clickhouse` |
| `EVNT_CLICKHOUSE__CONNECTION__PORT` | `8123` |
| `EVNT_CLICKHOUSE__CONNECTION__USERNAME` | `default` |
| `EVNT_CLICKHOUSE__CONNECTION__PASSWORD` | `password` (override in production) |
| `EVNT_CLICKHOUSE__CONNECTION__DATABASE` | `default`: the database the connection opens in, not where events are stored; see [databases, cluster and tables](#databases-cluster-and-tables) |
| `EVNT_CLICKHOUSE__CONNECTION__CONNECT_TIMEOUT` | `10` seconds to establish a connection |
| `EVNT_CLICKHOUSE__CONFIGURATION__DATABASE` | `evnt`: the database holding the event tables |
| `EVNT_CLICKHOUSE__CONFIGURATION__CLUSTER_NAME` | empty (single server); a cluster name switches to `ON CLUSTER` DDL and a Distributed table |
| `EVNT_CLICKHOUSE__CONFIGURATION__TABLES` | one `evnt` table group, as JSON; see [databases, cluster and tables](#databases-cluster-and-tables) |
| `EVNT_CLICKHOUSE__STARTUP_TIMEOUT_SECONDS` | `60` |
| `EVNT_CLICKHOUSE__STARTUP_RETRY_INTERVAL_MS` | `1000` between connection attempts during startup |
| `EVNT_INGEST__MODE` | `direct` (alternative: `rabbitmq`) |
| `EVNT_INGEST__DIRECT__ASYNC_INSERT` | `true`: ClickHouse buffers inserts server-side; see [direct inserts and durability](#direct-inserts-and-durability) |
| `EVNT_INGEST__DIRECT__WAIT_FOR_ASYNC_INSERT` | `false`: the request is answered before the buffer is flushed; see [direct inserts and durability](#direct-inserts-and-durability) |
| `EVNT_INGEST__RABBITMQ__HOST` | `rabbitmq` |
| `EVNT_INGEST__RABBITMQ__PORT` | `5672` |
| `EVNT_INGEST__RABBITMQ__USERNAME` | `guest` |
| `EVNT_INGEST__RABBITMQ__PASSWORD` | `guest` (override in production) |
| `EVNT_INGEST__RABBITMQ__VIRTUALHOST` | `/` |
| `EVNT_INGEST__RABBITMQ__CONNECT_TIMEOUT_SECONDS` | `5` per connection attempt, and for each readiness check |
| `EVNT_INGEST__RABBITMQ__STARTUP_TIMEOUT_SECONDS` | `60` |
| `EVNT_INGEST__RABBITMQ__STARTUP_RETRY_INTERVAL_MS` | `1000` between connection attempts during startup |
| `EVNT_INGEST__RABBITMQ__QUEUE_NAME` | `evnt.ingest` |
| `EVNT_INGEST__RABBITMQ__FAILED_QUEUE_NAME` | unset: `<queue_name>.failed`, so `evnt.ingest.failed`. Must differ from the main queue |
| `EVNT_INGEST__RABBITMQ__PREFETCH_COUNT` | `500` unacknowledged messages the broker hands the worker at once |
| `EVNT_INGEST__RABBITMQ__BATCH_SIZE` | `500` |
| `EVNT_INGEST__RABBITMQ__BATCH_TIMEOUT_MS` | `1000`: the worker flushes a partial batch after this long without a new message |
| `EVNT_INGEST__RABBITMQ__RETRY_DELAY_MS` | `1000`: the worker's wait after the first failed insert; it doubles with each further failure, up to a cap |
| `EVNT_INGEST__RABBITMQ__INSERT_TIMEOUT_SECONDS` | `60` |
| `EVNT_INGEST__RABBITMQ__PUBLISH_TIMEOUT_SECONDS` | `5` |
| `EVNT_INGEST__RABBITMQ__MAX_INFLIGHT_PUBLISHES` | `200` unconfirmed messages per process (up to ~180 KB each when merged) |
| `EVNT_INGEST__RABBITMQ__COALESCE_WINDOW_MS` | `5` (`0` publishes one message per request) |
| `EVNT_INGEST__RABBITMQ__COALESCE_MAX_ROWS` | `100` |
| `EVNT_INGEST__RABBITMQ__MAX_INSERT_ATTEMPTS` | `10` attempts before a message that keeps failing while other inserts succeed is moved to the failed queue (`0` retries forever) |

On startup the app (and, in `rabbitmq` mode, the worker) retries the ClickHouse connection until `startup_timeout_seconds` elapses.

### Direct inserts and durability

In `direct` mode the collector inserts every request into ClickHouse itself,
with the settings from `EVNT_INGEST__DIRECT__`:

| `ASYNC_INSERT` | `WAIT_FOR_ASYNC_INSERT` | What a `204` means |
| --- | --- | --- |
| `true` (default) | `false` (default) | ClickHouse accepted the rows into its async-insert buffer. They are not yet written to the table. |
| `true` | `true` | ClickHouse flushed the buffer holding the rows to the table. |
| `false` | ignored | ClickHouse wrote the rows as a part of their own: one part per request. |

With the defaults the collector answers before ClickHouse flushes. An error
at flush time (rows ClickHouse rejects while parsing them, a full disk, a
table that no longer matches) is invisible to the collector and the tracker:
the request already got its `204`, nothing is retried, and the rows are gone.
Rows still in the buffer are also lost if the ClickHouse server stops before
flushing them. Such failures show up only in ClickHouse, in
`system.asynchronous_insert_log` and the server log.

Set `EVNT_INGEST__DIRECT__WAIT_FOR_ASYNC_INSERT=true` to answer only after the
flush, so a flush error fails the request with a `5xx` and the tracker
resends it. Each request then waits for the flush (bounded by ClickHouse's
async-insert busy timeout), which adds latency and lowers the request rate
one collector process can sustain. `EVNT_INGEST__DIRECT__ASYNC_INSERT=false`
creates a part per request and suits low volume only: at a high request rate
ClickHouse answers `TOO_MANY_PARTS`.

The RabbitMQ worker uses the same two settings but always waits for the flush
when async inserts are on, whatever `WAIT_FOR_ASYNC_INSERT` says: it
acknowledges a message only after its rows are in the table.

### Databases, cluster and tables

Two settings name a database, and they do different jobs:

- `EVNT_CLICKHOUSE__CONNECTION__DATABASE` (default `default`) is the database
  the client connection opens in. It must already exist, because `evnt db
  init` connects through it before it creates anything. The collector always
  writes table names with their database, so this setting does not decide
  where events go.
- `EVNT_CLICKHOUSE__CONFIGURATION__DATABASE` (default `evnt`) is the database
  that `evnt db init` creates and that holds the event tables. Inserts go to
  `<database>.<table>`: `evnt.local` by default.

`EVNT_CLICKHOUSE__CONFIGURATION__CLUSTER_NAME` is empty for a single server.
With a cluster name, `evnt db init` runs its DDL `ON CLUSTER`, also creates a
`Distributed` table over the local one (`evnt.distributed`), and the collector
and the worker insert into the Distributed table instead of the local one.
Both names accept letters, digits and underscores only.

`EVNT_CLICKHOUSE__CONFIGURATION__TABLES` is a JSON object of table groups. The
collector uses the `evnt` group:

```bash
EVNT_CLICKHOUSE__CONFIGURATION__TABLES='{"evnt": {"enabled": true,
  "local": {"name": "local", "engine": "MergeTree()",
    "partition_by": "toYYYYMM(time)",
    "order_by": "app, platform, app_id, event_type, toDate(time), event.category, event.action, page, device_id, cityHash64(device_id), session_id, time",
    "sample_by": "cityHash64(device_id)",
    "settings": "index_granularity = 8192"},
  "distributed": {"name": "distributed", "sample_by": "cityHash64(device_id)"}}}'
```

That is the default. An override replaces the whole object rather than being
merged into the default, so always pass every key shown above; this is also
true of the nested form (`EVNT_CLICKHOUSE__CONFIGURATION__TABLES__EVNT__LOCAL__ENGINE=...`),
which leaves a group holding that one key and makes `evnt db init` fail. A
`name` containing a dot (`other_db.events`) places that table in another
database, which `evnt db init` creates too. The engine, partitioning, sorting
key and table settings are used only when `evnt db init` creates a table that
does not exist yet: they never alter an existing table. The collector, the
worker and `evnt db init` must all run with the same value.

### RabbitMQ delivery and failures

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
`x-evnt-error-type`, `x-evnt-failure` (`deterministic`,
`max_insert_attempts` or `undecodable`), `x-evnt-error-code` when ClickHouse
returned one, and `x-evnt-insert-attempts` for messages moved after
`max_insert_attempts`. `x-evnt-error` is cut to 2 KB. A message the worker
could not decode (`undecodable`) is moved with its body unchanged and has no
`x-evnt-table-group`; see [the failed queue](../run/queue.md#failed-queue).

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
