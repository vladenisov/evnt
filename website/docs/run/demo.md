# Demo SPA

Open **<http://localhost:8000/demo/>**. The demo SPA has three tabs:

- **Live Events** — every payload sent to `/tracker` is intercepted and rendered as an expandable JSON tree, with timestamp and method.
- **ClickHouse Tables** — TanStack Table grid that queries ClickHouse over HTTP **directly from the browser** (CORS is preconfigured in `deploy/clickhouse/`). Pick a table, sort, paginate, expand JSON columns inline.
- **Settings** — change the ClickHouse URL / user / password if you’re pointing at a non-default cluster; values persist in `localStorage`.

To skip the SPA in your image, build with `BUILD_DEMO=false` (also disable the runtime mount with `EVNT_COMMON__DEMO=false`):

```bash
EVNT_BUILD_DEMO=false docker compose build app
# or
docker build --build-arg BUILD_DEMO=false -t evnt .
```

`EVNT_COMMON__DEMO=true` enables the demo at runtime; it is disabled by default
outside the sample compose stack. Local builds go into `frontend/dist` and the
image serves `/app/demo`, both exposed under `/demo/`.

The ClickHouse URL in the demo must be reachable **from the browser**, not only
from Docker. Demo credentials are persisted in browser local storage. Use a
dedicated account with the permissions required for browsing your demo tables.
