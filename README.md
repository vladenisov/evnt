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

git submodule update --init --depth 1   # Iglu schemas; the image build requires them

# 1. Bring up ClickHouse first (the app waits for it).
docker compose up -d clickhouse

# 2. One-time: create the `evnt` database and tables (idempotent).
docker compose run --rm app evnt db init

# 3. Start the collector (add `--profile rabbitmq` for RabbitMQ mode).
docker compose up -d
```

For development (watch mode, tests, linters) see [CONTRIBUTING.md](CONTRIBUTING.md);
`make` lists every shortcut.

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

The Docker image already ships the official Snowplow JS bundle, served
first-party at `/static/sp/sp.js`. Outside the image, fetch it with:

```bash
cd backend && uv run evnt scripts download
```

That places `sp.js` (and plugins) into `backend/static/sp/` (the
`EVNT_COMMON__STATIC_DIR` directory, `/app/static` in the image). The vendored
subtree is gitignored and regenerated on every image build.

## Running the image

```bash
docker run -d -p 8000:8000 \
  -e EVNT_CLICKHOUSE__CONNECTION__HOST=clickhouse.internal \
  -e EVNT_CLICKHOUSE__CONNECTION__PASSWORD=... \
  vladenisov/evnt:latest
docker run --rm -e EVNT_CLICKHOUSE__CONNECTION__HOST=... vladenisov/evnt evnt db init
```

The container runs as the unprivileged `evnt` user (uid 1000) and listens on
**port 8000**. Its `HEALTHCHECK` polls `/live`; point load-balancer readiness
checks at `/`, which also checks the ingest backend. Every `evnt` CLI command
is available inside the image (`docker run --rm vladenisov/evnt evnt settings`).

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
evnt settings          # from backend/: uv run evnt settings
```

A starter [`.env.example`](.env.example) lists the most common runtime variables.

### Performance tuning

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_PERFORMANCE__USER_AGENT_CACHE_SIZE` | `32768` | Maximum entries in each process-local User-Agent LRU cache. Set to `0` to disable caching. |
| `EVNT_PERFORMANCE__CPU_TASK_CONCURRENCY` | `8` | Maximum CPU-heavy helper calls submitted concurrently by each application process. |

Those offloads run on asyncio's default thread pool, which holds
`min(32, cpu_count + 4)` workers. Raising `CPU_TASK_CONCURRENCY` above that
number has no effect: the pool, not the limiter, becomes the bottleneck.

`/live` is a lightweight process liveness endpoint and does not query the active
ingest backend. The existing `/` probe remains the backend readiness check.
It is excluded from the access log by default via
`EVNT_PERFORMANCE__ACCESS_LOG_EXCLUDED_PATHS`; override that list to change it.

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
| `EVNT_SECURITY__MAX_REQUEST_BODY_BYTES` | `10485760` | Ceiling on any request body, enforced before the handler runs. Refused with `413`. The encrypted endpoint applies its own, much tighter limit on top. |

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
| `EVNT_PROXY__DOMAINS` | `["google-analytics.com", "www.googletagmanager.com"]` | Hostname allowlist: `/proxy/route` fetches only from these hosts, and `/proxy/hash` rewrites only their URLs. |
| `EVNT_PROXY__ALLOWED_PORTS` | `[80, 443]` | Outbound ports the proxy may reach on an allowlisted host. A target with no explicit port (the scheme default) is always permitted; any other port is rejected with `403`. |

Redirects are **not** followed, so an allowlisted host cannot bounce the proxy to an internal target.

### Encrypted ingest (`/e`)

`/e` is the sealed-payload twin of `/tracker`. Clients encrypt the **same** Snowplow JSON body with the collector's public key; only the collector holds the private key. Nothing else changes — the payload model, parsing, and ClickHouse rows are identical.

The endpoint is off by default and is only mounted when enabled, so deployments that do not use it expose no extra surface.

```bash
uv sync --extra crypto          # from backend/; cryptography is optional
uv run evnt keys generate --kid=k1
```

That prints a public key to embed in your clients and a private key for the collector:

```bash
EVNT_ENCRYPTION__ENABLED=true
EVNT_ENCRYPTION__KEYS=[{"kid":"k1","private_key":"<base64>"}]
```

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_ENCRYPTION__ENABLED` | `false` | Mounts `/e` when true. |
| `EVNT_ENCRYPTION__ENDPOINT` | `/e` | Serves `POST`, `GET`, and `OPTIONS`. |
| `EVNT_ENCRYPTION__KEYS` | `[]` | List of `{kid, private_key}` or `{kid, private_key_file}`, plus optional `enabled`. |
| `EVNT_ENCRYPTION__MAX_ENVELOPE_BYTES` | `262144` | POST bodies above this get `413`. Capped at 8 MiB. |
| `EVNT_ENCRYPTION__MAX_PLAINTEXT_BYTES` | `1048576` | Ceiling after decompression, so a compression bomb cannot exhaust memory. Capped at 64 MiB, and at 16x the envelope ceiling. |
| `EVNT_ENCRYPTION__MAX_QUERY_BYTES` | `8192` | Separate, much smaller ceiling for the `GET` form, which every proxy caps near 8 KB anyway. |

Private keys are `SecretStr` and stay out of config dumps and logs. Prefer `private_key_file` to mount the key as a secret rather than exposing it in the process environment.

#### Scheme

Sealed box: **ephemeral X25519 → HKDF-SHA256 → AES-256-GCM**.

- **X25519** is native on every target — iOS CryptoKit (13+), Android via Tink or Conscrypt, Web via WebCrypto or `@noble/curves` (~8 KB). Fixed 32-byte keys, no ASN.1, no curve-point validation.
- **AES-256-GCM** is the only AEAD WebCrypto exposes natively, and is hardware-accelerated on current phones.
- **A fresh ephemeral key per event** gives a per-event content key, so a nonce can never be reused. A static client key would buy no authentication anyway — it would ship inside the app bundle.

Envelope, `67 + len(kid)` bytes of overhead, sent raw as `application/octet-stream` or base64 as text:

```
offset  size  field
0       4     magic      "EVN1"
4       1     version    0x01
5       1     flags      bit0 = plaintext deflated before sealing
                         (gzip or zlib container — both accepted)
6       1     kid_len    1..32
7       N     kid        ASCII key id
7+N     32    epk        ephemeral X25519 public key
39+N    12    nonce      AES-GCM nonce
51+N    ..    ct         ciphertext || 16-byte tag

shared = X25519(ephemeral_secret, recipient_public)
key    = HKDF-SHA256(ikm=shared, salt="", info="evnt/e/v1" || epk || recipient_public, 32)
aad    = envelope[0 : 39+N]
```

`backend/src/evnt/crypto.py::seal_envelope` is the executable specification — a client implementation is correct exactly when it produces envelopes that function would produce. It emits gzip, but the collector auto-detects the container, so a client using Android's `Deflater` (zlib) rather than `GZIPOutputStream` (gzip) interoperates without changes.

Unsealing runs inline on the event loop: a typical batch measures ~0.04 ms and the 1 MiB ceiling ~0.7 ms, which is why the ceilings above are what bound per-request cost. Raising them raises that cost proportionally.

The `kid` travels in cleartext, so several key pairs stay live at once and rotation needs no client flag day: add the new key, ship clients that use it, then drop the old one.

#### Client wiring

Mobile trackers hook in by replacing the network layer — Snowplow's [`NetworkConnection`](https://docs.snowplow.io/docs/sources/mobile-trackers/configuring-how-events-are-sent/?platform=android#configuring-the-network-connection) — so the tracker still builds ordinary Snowplow payloads and only the transport changes. Seal the request body, POST it to `/e` as `application/octet-stream`, and treat `204` as success. `GET /e?d=<base64url envelope>` returns a tracking pixel for transports that cannot POST; query-string limits make it suitable for single events only.

#### What this does and does not protect

It keeps payloads unreadable to anything between the client and the collector, including a TLS-terminating proxy or an on-device interceptor reading plaintext traffic.

It is **not** client authentication and **not** replay protection: the public key ships inside the app, so anyone who extracts it can seal valid payloads, and a captured envelope can be resent. Deduplicate downstream on `event_id` if that matters. Every rejection — bad key id, failed tag, malformed JSON, schema violation — returns the same opaque `400`, so the endpoint cannot be used as an oracle; the real reason is in the server log.

### Secrets

`EVNT_CLICKHOUSE__CONNECTION__PASSWORD` and `EVNT_INGEST__RABBITMQ__PASSWORD` are stored as Pydantic `SecretStr`: they are still configured the same way via environment variables, but their values are redacted from config dumps (`evnt settings`) and logs.

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
| `EVNT_INGEST__RABBITMQ__INSERT_TIMEOUT_SECONDS` | `60` |

On startup the app (and, in `rabbitmq` mode, the worker) retries the ClickHouse connection until `startup_timeout_seconds` elapses.

### RabbitMQ worker

In `rabbitmq` mode a separate worker drains the queue and batch-inserts into ClickHouse (`evnt queue worker`). It shuts down cleanly on `SIGTERM` (final flush + close), publishes to the failed queue with publisher confirms to avoid silent loss, times out and requeues a stuck ClickHouse insert, and backs off with capped exponential delay on downstream outages.

The worker writes a liveness file that a dedicated healthcheck reads:

```bash
evnt queue healthcheck
```

This is wired as the worker container `HEALTHCHECK` in [`compose.yml`](compose.yml); it reports unhealthy if the worker stops refreshing liveness. The staleness threshold stays above the worker's max backoff so a sustained backend outage is not misread as a dead worker.

## License & Attribution

This project's own source code is licensed under BSD 3-Clause (see [LICENSE](LICENSE)).

It interoperates with, and optionally redistributes unmodified copies of, third-party components from Snowplow Analytics Ltd. and other authors:

- **Snowplow JavaScript tracker** (`sp.js`, plugins) — BSD 3-Clause, © 2022 Snowplow Analytics Ltd, © 2010 Anthon Pang. Fetched by `evnt scripts download` at image build time; not committed to this repo.
- **Iglu Central schemas** — Apache License 2.0, © Snowplow Analytics Ltd. Included as a git submodule at `backend/vendor/iglu-central`, unmodified.

Full third-party copyright and license notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), which downstream packagers **must** redistribute alongside any Docker image or artifact that bundles the tracker scripts or Iglu schemas.

"Snowplow" is a trademark of Snowplow Analytics Ltd. This project is not affiliated with, sponsored by, or endorsed by Snowplow Analytics Ltd.
