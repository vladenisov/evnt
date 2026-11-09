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
