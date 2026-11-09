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

On startup the app (and, in `rabbitmq` mode, the worker) retries the ClickHouse connection until `startup_timeout_seconds` elapses.

## Secrets

`EVNT_CLICKHOUSE__CONNECTION__PASSWORD` and `EVNT_INGEST__RABBITMQ__PASSWORD` are stored as Pydantic `SecretStr`: they are still configured the same way via environment variables, but their values are redacted from config dumps (`evnt settings`) and logs.
