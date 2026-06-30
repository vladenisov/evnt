# evnt

`evnt` is a lightweight, self-hosted event collector that **implements the Snowplow tracker wire protocol**. Point any official Snowplow tracker (JS, iOS/Swift, Android/Kotlin, Python, etc.) at `evnt` and it will accept the events, enrich them, and write them to ClickHouse — no hosted Snowplow infrastructure required.

> **Disclaimer.** "Snowplow" is a trademark of Snowplow Analytics Ltd. This is an independent open-source project that interoperates with the publicly documented Snowplow tracker protocol and bundles the official Snowplow JavaScript tracker (BSD-3-Clause) and Iglu Central schemas (Apache-2.0) **unmodified**. It is **not affiliated with, sponsored by, or endorsed by Snowplow Analytics Ltd.** See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for full attribution.

## Why evnt

- **Snowplow-protocol compatible** — receive events from any official tracker without rewriting your client code.
- **ClickHouse-native** — events land in a wide, partitioned `MergeTree` table ready for sub-second analytics.
- **Lean stack** — FastAPI on Python 3.14, async ClickHouse client, no JVM, no Kafka requirement.
- **Optional durable buffer** — flip a flag to switch from direct writes to RabbitMQ + batch worker for high-load or flaky downstreams.
- **Self-hosted, no telemetry** — your data, your infra, your retention policy.
- **Built-in demo UI** — a Vue 3 single-page app at `/demo/` that shows the raw payloads as they leave the browser **and** lets you browse the ClickHouse tables directly from the front-end.

## Quickstart

```bash
git clone https://github.com/denisov-vlad/evnt.git
cd evnt

# 1. Bring up ClickHouse first (the app waits for it).
docker compose up -d clickhouse

# 2. One-time: create the `evnt` database and tables (idempotent).
docker compose run --rm app uv run python cli.py db init

# 3. Start the collector (and worker, if you want RabbitMQ mode).
docker compose up -d
```

Open **<http://localhost:8000/demo/>**. The demo SPA has three tabs:

- **Live Events** — every payload sent to `/tracker` is intercepted and rendered as an expandable JSON tree, with timestamp and method.
- **ClickHouse Tables** — TanStack Table grid that queries ClickHouse over HTTP **directly from the browser** (CORS is preconfigured in `deploy/clickhouse/`). Pick a table, sort, paginate, expand JSON columns inline.
- **Settings** — change the ClickHouse URL / user / password if you’re pointing at a non-default cluster; values persist in `localStorage`.

To skip the SPA in your image, build with `BUILD_DEMO=false` (the `/demo/` mount falls back to a placeholder):

```bash
EVNT_BUILD_DEMO=false docker compose build app
# or
docker build --build-arg BUILD_DEMO=false -t evnt .
```

## Sending events from your applications

`evnt` speaks the Snowplow tracker protocol. Use the official trackers — point their collector URL at `https://your-evnt-host` and they’ll just work. Reference docs:

- **JavaScript** (web) — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/javascript-trackers/>
- **Swift / iOS** — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/mobile-trackers/>
- **Kotlin / Android** — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/mobile-trackers/>
- **Python** — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/python-tracker/>

The full tracker matrix (Java, Go, .NET, Roku, Unity, Lua, …) is at <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/>.

If you want to host the official Snowplow JS bundle from your own domain, run:

```bash
uv run python evnt/cli.py scripts download
```

That places `sp.js` (and plugins) into `evnt/static/`, served at `/static/sp.js`.

## Configuration

Settings are loaded from a single Pydantic `BaseSettings` model. Use the `EVNT_` prefix and `__` for nested keys. List/dict values are parsed as JSON:

```bash
EVNT_COMMON__DEMO=true                          # enable the /demo/ SPA at runtime
EVNT_CLICKHOUSE__CONNECTION__HOST=clickhouse    # CH host
EVNT_INGEST__MODE=rabbitmq                      # direct (default) | rabbitmq
EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["https://example.com"]'
```

Inspect the full config tree (with defaults) any time:

```bash
uv run python evnt/cli.py settings
```

A starter [`.env.example`](.env.example) lists the most common runtime variables.

### Security-related defaults

The most important settings to know about:

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_SECURITY__DISABLE_DOCS` | `true` | `/docs`, `/redoc` and `/openapi.json` are **disabled**. Set to `false` to expose them. |
| `EVNT_SECURITY__CORS_ALLOWED_ORIGINS` | `["*"]` | JSON array of bare HTTP(S) origins (e.g. `["https://example.com"]`), or `["*"]`. |
| `EVNT_SECURITY__CORS_ALLOW_CREDENTIALS` | `true` | Credentialed CORS responses include `Access-Control-Allow-Credentials: true`. |
| `EVNT_SECURITY__TRUSTED_HOSTS` | `["*"]` | Allowed `Host` header values. |
| `EVNT_SECURITY__TRUST_PROXY_HEADERS` | `true` | When enabled, the client IP is taken from the configured proxy header (`X-Forwarded-For` by default). Set to `false` if `evnt` is exposed directly (no trusted reverse proxy) so clients cannot spoof their IP. |
| `EVNT_SECURITY__ENABLE_HTTPS_REDIRECT` | `false` | Adds an HSTS header and HTTPS redirect when enabled. |

**Re-enabling the API docs.** Interactive docs are off by default. To turn them back on (e.g. for a private/staging instance):

```bash
EVNT_SECURITY__DISABLE_DOCS=false
```

**CORS with credentials.** Browser credentials (cookies, `Authorization`) are allowed by default for collector compatibility. The default wildcard origin setting reflects the request `Origin` instead of returning `Access-Control-Allow-Origin: *`, so browser requests using `credentials: "include"` are accepted.

To restrict credentialed cross-origin requests to known frontends, list explicit origins:

```bash
EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["https://app.example.com"]'
EVNT_SECURITY__CORS_ALLOW_CREDENTIALS=true
```

To disable credentialed CORS while still accepting requests from any origin:

```bash
EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["*"]'
EVNT_SECURITY__CORS_ALLOW_CREDENTIALS=false
```

### Analytics-script proxy

The optional proxy at `/proxy` fetches allowlisted third-party analytics scripts so you can serve them first-party. It is constrained to prevent SSRF:

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_PROXY__DOMAINS` | `["google-analytics.com", "www.googletagmanager.com"]` | Hostname allowlist. |
| `EVNT_PROXY__PATHS` | `["analytics.js", "gtm.js"]` | Path allowlist. |
| `EVNT_PROXY__ALLOWED_PORTS` | `[80, 443]` | Outbound ports the proxy may reach on an allowlisted host. A target with no explicit port (the scheme default) is always permitted; any other port is rejected with `403`. |

Redirects are **not** followed, so an allowlisted host cannot bounce the proxy to an internal target.

### Secrets

`EVNT_CLICKHOUSE__CONNECTION__PASSWORD` and `EVNT_INGEST__RABBITMQ__PASSWORD` are stored as Pydantic `SecretStr`: they are still configured the same way via environment variables, but their values are redacted from config dumps (`cli.py settings`) and logs.

### ClickHouse and RabbitMQ

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

On startup the app (and, in `rabbitmq` mode, the worker) retries the ClickHouse connection until `startup_timeout_seconds` elapses.

### RabbitMQ worker

In `rabbitmq` mode a separate worker drains the queue and batch-inserts into ClickHouse (`cli.py queue worker`). It shuts down cleanly on `SIGTERM` (final flush + close), publishes to the failed queue with publisher confirms to avoid silent loss, and backs off with capped exponential delay on downstream outages.

The worker writes a liveness file that a dedicated healthcheck reads:

```bash
uv run python evnt/cli.py queue healthcheck
```

This is wired as the worker container `HEALTHCHECK` in [`compose.yml`](compose.yml); it reports unhealthy if the worker stops refreshing liveness. The staleness threshold stays above the worker's max backoff so a sustained backend outage is not misread as a dead worker.

## License & Attribution

This project's own source code is licensed under BSD 3-Clause (see [LICENSE](LICENSE)).

It interoperates with, and optionally redistributes unmodified copies of, third-party components from Snowplow Analytics Ltd. and other authors:

- **Snowplow JavaScript tracker** (`sp.js`, plugins) — BSD 3-Clause, © 2022 Snowplow Analytics Ltd, © 2010 Anthon Pang. Fetched on demand by `cli.py scripts download`; not committed to this repo.
- **Iglu Central schemas** — Apache License 2.0, © Snowplow Analytics Ltd. Included as a git submodule at `evnt/vendor/iglu-central`, unmodified.

Full third-party copyright and license notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), which downstream packagers **must** redistribute alongside any Docker image or artifact that bundles the tracker scripts or Iglu schemas.

"Snowplow" is a trademark of Snowplow Analytics Ltd. This project is not affiliated with, sponsored by, or endorsed by Snowplow Analytics Ltd.
