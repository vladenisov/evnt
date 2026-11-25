# Upgrading from the legacy layout

This guide covers the transition from the former root-level Python project
to the packaged `evnt` runtime. The public collector and tracker asset URLs
remain the same; deployment commands and internal paths change.

## Deployment changes

- **Repository layout**: the service is now the `evnt` package under `backend/src/evnt` (src layout, tests inside the package) and the demo SPA lives in `frontend/`. Anything that imported modules by their old paths (`core.*`, `routers.*`, `evnt.core.*`) must use the new ones (`evnt.config`, `evnt.api.*`, `evnt.tracker.*`, `evnt.storage.clickhouse.*`).
- **Container port 80 → 8000**, and the image runs as the unprivileged `evnt` user (uid 1000) under tini. Update port mappings and load-balancer targets.
- **CLI**: `python cli.py ...` is replaced by the `evnt` console script (`evnt settings`, `evnt db init`, `evnt queue worker`, `evnt queue healthcheck`, `evnt scripts download`, `evnt keys generate|public`). Commands and arguments are unchanged.
- **Removed settings**: `EVNT_COMMON__DEBUG`, `EVNT_PERFORMANCE__DB_POOL_OVERFLOW` and `EVNT_PROXY__PATHS` had no effect and are gone. `EVNT_PROXY__PATHS` never restricted what the proxy fetches; it only decided which URLs `/proxy/hash` rewrote, and produced links the proxy then refused.
- **Removed**: the unregistered SendGrid handler, and the `json-repair` dependency (its model hook never ran: FastAPI parses request bodies itself, so malformed JSON was already a 422).

## Before rolling out

1. Update the container target port to `8000` in compose and proxy backends.
2. Replace command overrides with `uvicorn evnt.main:app` or the installed
   `evnt` CLI; the runtime image does not include `uv` or `curl`.
3. Use a Python HTTP healthcheck on `/live`, and backend readiness on `/`.
4. Initialize the configured ClickHouse database with `evnt db init` as an
   explicit step. API startup does not create tables.
5. In RabbitMQ mode, start `evnt queue worker` and configure its separate
   `evnt queue healthcheck`.

See [Docker deployment](docker.md), [configuration](../configure/settings.md),
and [GitHub Releases](https://github.com/vladenisov/evnt/releases) for operation
and version-specific changes. There is no separate manually maintained changelog.

## Original client IP and proxy country

Collector rows now preserve the original parsed IP in
`extra.collector.client_ip`, including native IPv6. `user_ip` stays IPv4 with its
existing conversion and unknown-address behavior. No new column or `ALTER TABLE`
is required; direct ingestion and RabbitMQ use the existing JSON column.

Country-header ingestion is opt-in. Behind a trusted Cloudflare origin, set:

```bash
EVNT_SECURITY__TRUST_PROXY_HEADERS=true
EVNT_COMMON__SNOWPLOW__USER_IP_HEADER=CF-Connecting-IP,X-Forwarded-For
EVNT_COMMON__SNOWPLOW__USER_COUNTRY_HEADER=CF-IPCountry
```

`USER_IP_HEADER` accepts a comma-separated list checked in order. Keep
`X-Forwarded-For` after `CF-Connecting-IP` when any hostname reaches the
collector through a proxy other than Cloudflare: with `CF-Connecting-IP` alone,
those requests carry no `CF-*` headers and their events lose the client IP
(`user_ip` becomes `0.0.0.0`, `client_ip` empty). Such requests also have no
`CF-IPCountry`; derive their country from the IP instead.

Use the original Cloudflare header, not `CF-Pseudo-IPv4`. Pseudo IPv4 **Add Header**
preserves the original address; **Overwrite Headers** does not. Ensure direct
clients cannot bypass the trusted proxy before trusting its headers.

After deploying an image with this change, query the new JSON paths rather than
inferring country from `user_ip = 0.0.0.0`:

```sql
SELECT
    extra.collector.client_ip.:String AS client_ip,
    extra.collector.ip_country.:String AS ip_country
FROM evnt.local
WHERE time >= now() - INTERVAL 1 HOUR
LIMIT 10;
```

The fields appear only on newly processed events. Older rows and messages already
in RabbitMQ do not gain lost IPv6 addresses or countries. Existing workers can
write the added JSON fields, so the database and queue layout remain compatible.
`XX` means unknown country and `T1` is Cloudflare's Tor marker, not an ISO country.

## Native async ClickHouse client

The backend now uses `clickhouse-connect[async,tzdata]` 1.x. Rebuild the image or
run `uv sync --locked --all-extras` from `backend/` to install the updated lockfile.
The async transport uses aiohttp; `EVNT_PERFORMANCE__DB_POOL_SIZE` limits concurrent
HTTP connections per process. It no longer sizes a urllib3 pool used by an executor.

The packaged timezone data supports minimal images without system timezone files.
Collector URLs, environment variable names, and ClickHouse column definitions
remain unchanged. UTC timestamps retain millisecond precision.

## Per-event validation

A bad event no longer fails its whole request. Before, one invalid element made
`/tracker` answer `422` for the entire batch (which the trackers then dropped
without retry), a pixel with an invalid parameter got `422`, a sealed batch
with an invalid event got `400`, and an event that crashed parsing (for
example a `screen_view` without `id`) turned the batch into a `500` that the
trackers resent forever. Now:

- Events are validated one at a time. An event with invalid or missing fields
  that can still be built is stored with field defaults and the problems listed
  in `extra.validation`; an event that cannot be built (not an object, no valid
  `e`) is dropped, logged and counted in `evnt_events_dropped_total`. The
  request gets its normal success response.
- `/tracker` still answers `422` to a body that is not a Snowplow payload, which
  now includes a JSON object without a `data` list (it was accepted as an empty
  batch). `/i` always answers with the pixel unless storage fails.
- `aid` and `res` are optional, `p=headset` is accepted (stored as `mob`), and
  `ttm` is read. A nil UUID no longer becomes a device or session id, and a
  malformed `userId`/`sessionId` in `client_session` no longer replaces the
  event's `duid`/`sid` with the zero UUID.

No column changes: everything new goes into the existing `extra` JSON column
(`extra.validation`, `extra.tracker`), and the RabbitMQ message layout is
unchanged, so messages queued by the previous version are processed as before.
See [responses and bad events](../integrate/http-api.md#responses-and-bad-events)
and [tracker compatibility](../integrate/trackers.md#tracker-compatibility).

## Context entities, tracker overrides and event fingerprint

- Context entities the collector has no column mapping for used to be dropped
  with a `Schema has no parser` warning (rate-limited, but the largest source
  of log lines). They are now kept in `extra.contexts`, and so are the second
  and later occurrences of a mapped schema on one event, which the columns
  merge or overwrite. The warning is gone. What goes into the existing columns
  is unchanged.
- `ua` is parsed instead of the `User-Agent` header when sent, so events with
  it now store that agent in `user_agent` and the parsed fields. `ip` replaces
  `user_ip` for `p=srv` events. Both, and `tnuid`, are kept in `extra.tracker`.
- Every event gets `extra.event_fingerprint` for finding resent copies.
- A `com.acme/static_context` key named `validation`, `tracker`, `contexts`,
  `event_fingerprint` or `collector`, or starting with one of them followed by
  a dot, is no longer merged into `extra`: those paths now belong to the
  collector. The dropped keys are named in `extra.validation`. Rename such keys
  in the tracker configuration if you relied on them.

No column changes, and the RabbitMQ message layout is unchanged: the new keys
are inside the existing `extra` JSON column, so messages queued by the previous
version are processed as before. See
[tracker compatibility](../integrate/trackers.md#tracker-compatibility) and
[context entities](../integrate/trackers.md#context-entities).

## Visitor location and `SP-Anonymous`

Two opt-in settings, both off by default, so nothing changes until they are set:

- `EVNT_COMMON__SNOWPLOW__CLOUDFLARE_GEO_HEADERS=true` stores Cloudflare's
  visitor location headers (city, region, coordinates, postal code, time zone
  and more) in `extra.collector.geo`. Turn on **Add visitor location headers**
  in Cloudflare's Managed Transforms first; proxy trust must be on.
- `EVNT_COMMON__SNOWPLOW__HONOR_SP_ANONYMOUS=true` makes requests with the
  trackers' `SP-Anonymous` header store no IP address (`user_ip` is `0.0.0.0`,
  the `ip` parameter is ignored), no country or location and no `_sp_id`
  cookie device id; their `extra.collector` is `{"anonymous": true}`. Rows
  from such requests have no `extra.collector.client_ip` or `ip_country`, so
  queries reading those paths see `NULL` rather than an empty string for them.

No column changes; the new keys are inside the existing `extra` JSON column and
the RabbitMQ message layout is unchanged. See
[visitor location](../configure/settings.md#visitor-location-cloudflare) and
[server anonymisation](../configure/settings.md#server-anonymisation-sp-anonymous).

## Failed queue, proxy origin and Elastic APM settings

- **Undecodable queue messages are kept.** A message the RabbitMQ worker cannot
  decode used to be rejected and lost. It is now moved to the failed queue with
  its body unchanged and `x-evnt-failure: undecodable`; see
  [the failed queue](queue.md#failed-queue). Monitor that queue's depth if you
  did not already.
- **`/proxy/hash` uses the request origin by default.** With
  `EVNT_COMMON__HOSTNAME` unset, the returned URLs used to start with
  `http://localhost:8000`; they now use the `Host` of the request and its
  scheme. Deployments that set `EVNT_COMMON__HOSTNAME` are unaffected. Behind a
  proxy that rewrites `Host` or terminates TLS without sending
  `X-Forwarded-Proto`, set it; see [the proxy](../configure/proxy.md).
- **The proxy relays caching headers.** `If-None-Match` and `If-Modified-Since`
  go upstream, and `Cache-Control`, `ETag`, `Last-Modified` and `Expires` come
  back, so a browser can get a `304` for a proxied script.
- **`EVNT_ELASTIC_APM__SERVER_URL` and `EVNT_ELASTIC_APM__SERVICE_NAME` now
  take effect.** Both were ignored: the agent reported to its own default
  (`http://127.0.0.1:8200` or `ELASTIC_APM_SERVER_URL`) under
  `EVNT_COMMON__SERVICE_NAME`. If either variable is set in your environment,
  check that it holds the value you want before upgrading.
- **`EVNT_CLICKHOUSE__TABLES` is removed.** It was never read; the table layout
  is `EVNT_CLICKHOUSE__CONFIGURATION__TABLES`. A leftover variable is ignored.
- **Unpaired surrogates are replaced.** A string holding half of a surrogate
  pair (a truncated emoji) made the whole request fail with a `500`. Each such
  character is now stored as U+FFFD and the column is named in
  `extra.validation`. No column changes.
