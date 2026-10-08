# Contributing to evnt

This file is the single source of truth for building, testing and linting. CI
(`.github/workflows/ci.yml`) and the root `Makefile` use these commands.
`make check` runs local lint, types, tests and the frontend build; CI also
enforces coverage and requires integration tests. Keep all three aligned.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for the backend. It installs the pinned
  Python (`backend/.python-version`, 3.14) itself.
- Bun 1.4.2 (`frontend/.bun-version`, also pinned in `packageManager`) for
  frontend dependencies, development and builds. Node 26
  (`frontend/.node-version`) runs vue-tsc and Vitest with V8 coverage.
- Docker with Compose v2, for the full stack and the integration tests.
- The Iglu schemas submodule: `git submodule update --init --depth 1`.

Never use `pip`, `poetry`, `npm` or `yarn` here: the lockfiles
(`backend/uv.lock`, `frontend/bun.lock`) are what CI installs.

```bash
make install    # submodule + uv sync + bun install --frozen-lockfile + pre-commit hooks
make            # list every target
```

## Local development

Full stack (collector, ClickHouse, demo UI), with backend sources synced into
the container and reloaded on change:

```bash
make dev        # docker compose up --watch
make db-init    # once: create the database and tables (idempotent)
```

The collector is on <http://localhost:8000>, the demo UI on
<http://localhost:8000/demo/>. Add the RabbitMQ buffer and its worker with:

```bash
EVNT_INGEST__MODE=rabbitmq docker compose --profile rabbitmq up --watch
```

Just the API on the host, with reload (needs a ClickHouse on localhost:8123):

```bash
make dev-be     # uv run uvicorn evnt.main:app --reload --port 8000
```

## Backend workflow

Everything runs from `backend/` through `uv run`, or from the root via `make`.
Install with `uv sync --all-extras` (what `make install-be` and CI do): mypy
only checks the optional integrations (APM, Sentry, crypto) when they are
installed, so a plain `uv sync` can pass locally and fail in CI.

| Gate | Command | Make |
| --- | --- | --- |
| Lint | `uv run ruff check && uv run ruff format --check` | `make lint-be` |
| Format | `uv run ruff check --fix && uv run ruff format` | `make format` |
| Types | `uv run mypy` (strict) | `make typecheck-be` |
| Tests | `uv run pytest` | `make test-be ARGS="-k proxy"` |

All tool configuration is in `backend/pyproject.toml`; no command takes path
arguments, so CI, `make` and the pre-commit hook always agree on scope.

Tests live inside the package, in `src/evnt/tests/`, mirroring the module they
cover (`api/`, `tracker/`, `storage/`, ...). Async tests use anyio
(`@pytest.mark.anyio`, asyncio backend). `tests/support.py` has
`build_app(monkeypatch)` for route tests against fake backends.

### Integration tests (real ClickHouse and RabbitMQ)

Unit tests fake both backends, so they cannot catch DDL ClickHouse rejects,
client options the driver does not accept, or rows that never arrive. The
tests in `src/evnt/tests/integration/` (marker `integration`) run against real
servers: `db init` idempotency, POST and GET requests landing in ClickHouse
through the real lifespan, and the RabbitMQ worker delivering.

They skip when the servers are unreachable. CI sets `EVNT_IT_REQUIRED=1`, which
turns a skip into a failure. Against the compose stack:

```bash
docker compose --profile rabbitmq up -d clickhouse rabbitmq
cd backend
EVNT_IT_REQUIRED=1 EVNT_IT_CLICKHOUSE_PASSWORD=password uv run pytest -m integration
```

Connection settings: `EVNT_IT_CLICKHOUSE_HOST/_PORT/_USER/_PASSWORD` (default
`localhost:8123`, `default`, empty password) and
`EVNT_IT_RABBITMQ_HOST/_PORT/_USER/_PASSWORD` (default `localhost:5672`,
`guest`/`guest`). Each session uses a throwaway database and queue, so the
tests never touch real data.

## Frontend workflow

From `frontend/`, or from the root via `make`:

| Gate | Command | Make |
| --- | --- | --- |
| Dev server | `bun run dev` | `make dev-fe` |
| Lint | `bun run lint` (oxlint) | `make lint-fe` |
| Types | `bun run typecheck` (vue-tsc) | `make typecheck-fe` |
| Tests | `bun run test` (vitest) | `make test-fe` |
| Build | `bun run build` | `make build-fe` |

The backend serves the production build under `/demo/`, so Vite builds with
that base path. `bun run build` writes `frontend/dist/`, which is what an API
started from `backend/` with `EVNT_COMMON__DEMO=true` serves.

Use `bun run test`, not `bun test`: the suite uses Vitest and Vue transforms.
vue-tsc requires Node compiler hooks; Vitest uses Node for V8 coverage.
`bun run build` runs Vite on Bun; `make check` also runs typechecking first.

## Coverage

Backend: `uv run pytest --cov=evnt`, floor in `[tool.coverage.report]
fail_under` in `backend/pyproject.toml`. Frontend: `bun run test:coverage`, with
thresholds in `vite.config.ts`. Floors only go up: raise them when coverage
rises, never lower them to get a change through.

## Docker image

```bash
make image      # docker build -t evnt:local .
```

Build args: `EXTRAS` (space-separated optional dependencies, e.g.
`"apm sentry crypto"`) and `BUILD_DEMO=false` to skip the Bun stage. The build
fails on purpose when the Iglu submodule is not checked out: without the
schemas, validation would silently be skipped.

## Releases

`release.yml` runs the whole CI suite and then publishes `vladenisov/evnt` for
linux/amd64 and linux/arm64: `:latest` on every push to `main`, `:X.Y.Z` and
`:X.Y` for a `vX.Y.Z` tag. Bump `version` in `backend/pyproject.toml` and move
the CHANGELOG `[Unreleased]` section under the new version before tagging.

## Pull requests

- One logical change per commit, message `<type>: <description>` (`feat`,
  `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`, `build`).
- `make check` passes locally; a bug fix comes with the test that would have
  caught it.
- User-visible changes, and every breaking one, get a CHANGELOG entry.
