# AGENTS.md

## What This Repo Is

`evnt` is a lightweight self-hosted event collector that implements the Snowplow tracker wire protocol so it can receive events from the upstream Snowplow JavaScript and mobile trackers. It is an independent project, not affiliated with Snowplow Analytics Ltd.

Use it to:
- receive tracker requests over HTTP using the documented Snowplow protocol;
- write events into ClickHouse, directly or through a RabbitMQ buffer;
- accept sealed (encrypted) payloads on `/e`;
- proxy allowlisted third-party analytics scripts first-party;
- serve the demo SPA when demo mode is enabled.

This file is the fast operational guide for agents. Build, test and lint commands live in **[CONTRIBUTING.md](CONTRIBUTING.md)**, the single source of truth; user-facing configuration lives in **[README.md](README.md)**.

## Toolchain Rules

- Backend: `uv` only (`backend/uv.lock`). Frontend: `pnpm` only (`frontend/pnpm-lock.yaml`). Never `pip`, `poetry`, `npm` or `yarn`: the lockfiles drift from CI.
- Run backend commands from `backend/` (`uv run pytest`, `uv run evnt ...`), or use the root `Makefile` (`make check` runs every gate CI runs).
- The CLI is the `evnt` console script (`uv run evnt settings`, `evnt db init`, `evnt queue worker`, `evnt queue healthcheck`, `evnt scripts download`, `evnt keys generate`). There is no `cli.py` to run by path.

## Layout

```
backend/
  pyproject.toml, uv.lock        package `evnt`, src layout, all tool config
  vendor/iglu-central/           git submodule: Iglu schemas (validation data)
  src/evnt/
    main.py                      create_app(): middleware, routers, static, demo
    lifespan.py                  startup/shutdown on an AsyncExitStack
    config.py, constants.py      EVNT_* settings model and defaults
    cli.py                       the `evnt` CLI (Fire)
    health.py, crypto.py         readiness probe; sealed-envelope scheme
    api/                         HTTP routers: tracker, encrypted, proxy, health; deps.py
    tracker/                     Snowplow payload models, parsing, Iglu, UA, IP
    storage/clickhouse/          client factory, connector, DDL, field registry
    ingest/rabbitmq.py           publisher + batch worker
    middleware/, observability/  raw-ASGI middleware; logging, tracing, APM
    tests/                       mirrors the package; tests/integration needs real backends
frontend/                        Vue 3 demo SPA (Vite, pnpm), served at /demo/
deploy/clickhouse/               ClickHouse config mounted by compose.yml
Dockerfile, compose.yml, Makefile
```

## Runtime Facts

- Readiness probe is `GET /` (checks the active ingest backend); `GET /live` is process liveness only. There is no `/health`.
- Collector endpoints default to `/tracker` (POST batches) and `/i` (GET pixel); `/e` exists only when `EVNT_ENCRYPTION__ENABLED=true`; the proxy is under `/proxy`.
- The app does not create tables. `evnt db init` does, explicitly and idempotently.
- Ingest mode is `direct` by default; `rabbitmq` needs the separate `evnt queue worker` process.
- The container listens on 8000 as the non-root `evnt` user.
- `/demo/` exists only when `EVNT_COMMON__DEMO=true`; it serves `common.demo_dir` (`../frontend/dist` from `backend/`, `/app/demo` in the image).
- Lifespan warms the local Iglu schema cache from `common.snowplow.iglu_schemas_dir`. Without the submodule, unknown schemas validate as skipped, not failed, which is why the image build refuses an empty schema dir.

## Guardrails

- External contracts are stable: the `EVNT_` env prefix with `__` nesting, the endpoint paths above, and the ClickHouse column layout. Changing any of them is a breaking change and goes in CHANGELOG.md.
- Keep README tables, `.env.example`, `compose.yml` and code defaults aligned when touching configuration.
- Unit tests fake ClickHouse and RabbitMQ. If a change depends on real backend behaviour (DDL, insert settings, queue semantics), cover it in `tests/integration` and run it (CONTRIBUTING.md, "Integration tests"). Mocks in this repo have hidden real bugs before.
- When touching request parsing, check both `/tracker` and `/i`: the POST body and the GET query string bind the same model through different FastAPI paths.
- Keep the proxy routes out of scope unless the task requires them.
