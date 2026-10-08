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
