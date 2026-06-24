# --- Stage 1: build the Vue demo SPA ---
# Toggle via `--build-arg BUILD_DEMO=false` (or EVNT_BUILD_DEMO=false in compose).
# When false, a tiny placeholder index.html is shipped instead of the real SPA,
# so FastAPI's StaticFiles mount has something to serve.
ARG BUILD_DEMO=false

FROM node:24-alpine AS web-builder-true
WORKDIR /web
RUN corepack enable
COPY evnt/routers/demo/web/package.json evnt/routers/demo/web/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --config.package-import-method=copy
COPY evnt/routers/demo/web/ ./
RUN pnpm run build

FROM alpine:3 AS web-builder-false
WORKDIR /web
RUN mkdir -p dist && \
    printf '%s\n' \
        '<!doctype html><meta charset="utf-8"><title>evnt</title>' \
        '<p>Demo UI is disabled in this image (BUILD_DEMO=false).</p>' \
        '<p>Rebuild with <code>--build-arg BUILD_DEMO=true</code> to enable.</p>' \
        > dist/index.html

# Pick the stage matching BUILD_DEMO ("true" or "false").
FROM web-builder-${BUILD_DEMO} AS web-builder

# --- Stage 2: runtime image ---
FROM python:3.14.4-alpine3.23

COPY --from=ghcr.io/astral-sh/uv:0.11.8-python3.14-alpine3.23 /usr/local/bin/uv /usr/local/bin/uv

# Space-separated list of optional dependency groups to install, e.g.
#   docker build --build-arg EXTRAS="apm sentry" .
# Corresponds to [project.optional-dependencies] in pyproject.toml.
ARG EXTRAS=""
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON=python3.14

WORKDIR /app

# Install build dependencies and runtime libraries.
RUN --mount=type=cache,id=root-cache-${TARGETOS}-${TARGETARCH}${TARGETVARIANT},sharing=locked,target=/root/.cache \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    apk update && \
    apk add --no-cache \
        g++ \
        libgcc \
        curl \
        tini && \
    extra_args="" && \
    for extra in ${EXTRAS}; do extra_args="${extra_args} --extra ${extra}"; done && \
    uv sync --locked --no-install-project ${extra_args} && \
    apk del g++ && \
    rm -rf /var/cache/apk/*

WORKDIR /app/evnt

COPY ./evnt /app/evnt
COPY --from=web-builder /web/dist /app/evnt/routers/demo/web/dist
COPY LICENSE THIRD_PARTY_NOTICES.md /app/

RUN --mount=type=cache,id=root-cache-${TARGETOS}-${TARGETARCH}${TARGETVARIANT},sharing=locked,target=/root/.cache \
    uv run cli.py scripts download --version 4.6.9 --output_dir static --force

# Use tini as init to properly handle signals
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
