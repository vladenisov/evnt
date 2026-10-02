# evnt demo SPA

A small Vue 3 app for trying the collector by hand. It loads the Snowplow JS
tracker from the collector (`/static/sp/sp.js`), fires events at it, shows
every request the tracker sends (the **Live Events** tab), and browses the
ClickHouse tables straight from the browser over ClickHouse's HTTP interface
with `@clickhouse/client-web` (the **ClickHouse Tables** tab). Nothing goes
through a backend proxy.

The backend serves the built app at `/demo/` when `EVNT_COMMON__DEMO=true`
(from `EVNT_COMMON__DEMO_DIR`, default `../frontend/dist`). The app is built
with Vite `base: "/demo/"`, and the router takes its base from the same value.

## Requirements

- Node 26 (`.node-version`) and pnpm 11 (pinned in `package.json`
  `packageManager`).
- A running collector with the tracker downloaded (`evnt scripts download`
  puts it under `static/sp`).
- ClickHouse reachable from the browser with CORS enabled; the configs in
  `deploy/clickhouse/` do that for local use. Connection settings live on the
  **Settings** tab: URL, user and database are kept in `localStorage`, the
  password only in `sessionStorage`.

## Development

```sh
pnpm install
pnpm dev            # http://localhost:5173/demo/
```

The tracker sends to the page's own origin, so the dev server proxies the
collector paths (`/tracker`, `/i`, `/e`, `/e.js`, `/static/`) to
`http://localhost:8000`. Point it at another collector with
`VITE_PROXY_TARGET=http://host:port pnpm dev`. ClickHouse is not proxied; the
browser talks to the URL on the Settings tab (default `http://localhost:8123`).

## Scripts

| Script               | What it does                                       |
| -------------------- | -------------------------------------------------- |
| `pnpm dev`           | Vite dev server with the collector proxy           |
| `pnpm build`         | Type-check (`vue-tsc -b`) and build into `dist/`   |
| `pnpm preview`       | Serve the production build locally                 |
| `pnpm lint`          | oxlint, warnings fail                              |
| `pnpm typecheck`     | `vue-tsc -b` over the app, tests and Vite config   |
| `pnpm test`          | Vitest (happy-dom)                                 |
| `pnpm test:coverage` | Vitest with v8 coverage and per-directory floors   |
