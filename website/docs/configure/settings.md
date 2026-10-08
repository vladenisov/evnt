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
