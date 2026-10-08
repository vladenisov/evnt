# syntax=docker/dockerfile:1
#
# Single-container image: the collector API, the self-hosted Snowplow tracker
# under /static and, optionally, the demo SPA under /demo. Build from the repo
# root, with the iglu-central submodule checked out:
#
#     git submodule update --init
#     docker build -t evnt .
#
# Build args:
#   EXTRAS      space-separated optional dependency groups, e.g. "apm sentry crypto"
#   BUILD_DEMO  "false" ships a placeholder page instead of building the SPA

ARG BUILD_DEMO=true
ARG PYTHON_IMAGE=python:3.14-alpine3.23
ARG BUN_VERSION=1.4.2

# ---- demo SPA -> /web/dist ----
# Bun matches frontend/.bun-version and package.json "packageManager".
FROM oven/bun:${BUN_VERSION}-alpine AS demo-true
WORKDIR /web
COPY frontend/package.json frontend/bun.lock ./
RUN --mount=type=cache,id=bun-cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile
COPY frontend/ ./
RUN bun run build

FROM alpine:3 AS demo-false
RUN mkdir -p /web/dist && printf '%s\n' \
        '<!doctype html><meta charset="utf-8"><title>evnt</title>' \
        '<p>The demo UI is not part of this image (BUILD_DEMO=false).</p>' \
        > /web/dist/index.html

FROM demo-${BUILD_DEMO} AS demo

# ---- backend virtualenv, tracker download, Iglu schemas ----
FROM ${PYTHON_IMAGE} AS backend
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ARG EXTRAS=""
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# Some dependencies ship no musl wheel for every architecture.
RUN apk add --no-cache build-base
WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    extra_args=""; for extra in ${EXTRAS}; do extra_args="${extra_args} --extra ${extra}"; done; \
    uv sync --locked --no-dev --no-install-project ${extra_args}
COPY backend/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    extra_args=""; for extra in ${EXTRAS}; do extra_args="${extra_args} --extra ${extra}"; done; \
    uv sync --locked --no-dev ${extra_args}

# A payload whose Iglu schema is missing from disk validates as `skipped`
# instead of failing, so an image built from a checkout without the submodule
# would quietly run with schema validation off. Refuse to build one.
COPY backend/vendor/iglu-central/schemas ./iglu/schemas
RUN test -n "$(ls -A iglu/schemas)" || { \
        echo "backend/vendor/iglu-central is empty: run 'git submodule update --init'" >&2; \
        exit 1; \
    }

RUN .venv/bin/evnt scripts download --output_dir=/app/static/sp

# ---- runtime ----
FROM ${PYTHON_IMAGE} AS runtime
RUN apk add --no-cache libgcc libstdc++ tini \
    && addgroup -S -g 1000 evnt \
    && adduser -S -D -H -u 1000 -G evnt evnt
WORKDIR /app

COPY --from=backend /app/.venv ./.venv
COPY --from=backend /app/src ./src
COPY --from=backend /app/iglu ./iglu
COPY --from=backend /app/static ./static
COPY --from=demo /web/dist ./demo
COPY LICENSE THIRD_PARTY_NOTICES.md ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EVNT_COMMON__STATIC_DIR=/app/static \
    EVNT_COMMON__DEMO_DIR=/app/demo \
    EVNT_COMMON__SNOWPLOW__IGLU_SCHEMAS_DIR=/app/iglu/schemas

# Unprivileged, so the app listens on 8000 rather than 80.
USER evnt
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/live')"]

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["uvicorn", "evnt.main:app", "--host", "0.0.0.0", "--port", "8000"]
