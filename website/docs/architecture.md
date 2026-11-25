# Architecture

`evnt` implements the Snowplow tracker wire protocol independently of Snowplow
Analytics Ltd. It receives web/mobile tracker requests, converts them into a
shared row model, and delivers those rows to ClickHouse.

## Request path

1. The middleware applies host/HTTPS rules, CORS, request body limits,
   correlation IDs, logging, and response security headers.
2. `/tracker` binds a batch envelope from JSON; `/i` reads one event from query
   parameters. `/e` unseals the same envelope when encryption is enabled.
3. Shared processing validates every event on its own, so a bad event costs
   only itself, extracts IP and User-Agent data, parses contexts and
   unstructured events, and validates known Iglu schemas.
4. The active row sink writes directly to ClickHouse or publishes to RabbitMQ.

Schema validation is best-effort: a warning or an unknown schema does not make
an otherwise valid tracker request fail. Data that fails a known schema is
listed in the event's `extra.validation`. Entities without a column mapping
are kept in `extra.contexts`. The image requires the Iglu submodule so known
schemas are not silently missing.

## Ingest modes

| Mode | Request acknowledgement | Separate worker |
| --- | --- | --- |
| `direct` | After ClickHouse accepts the rows into its async-insert buffer, by default before they are flushed to the table | No |
| `rabbitmq` | After publishing to the queue; ClickHouse delivery happens later | `evnt queue worker` |

In `direct` mode the defaults are `async_insert=1` and
`wait_for_async_insert=0`: the `204` is sent before ClickHouse flushes the
rows, so an error at flush time is invisible to the client and those rows are
lost without a retry. `EVNT_INGEST__DIRECT__WAIT_FOR_ASYNC_INSERT=true`
answers only after the flush, at the cost of latency. See
[direct inserts and durability](configure/settings.md#direct-inserts-and-durability).

Startup connects only the active API ingest backend, creates the proxy HTTP
client, loads encryption keys, and warms known schema validators. Resources
are closed on shutdown or if a later startup step fails.

Tables are never created implicitly by the API. Operators run `evnt db init`
explicitly before ingestion.

## Repository map

| Directory | Responsibility |
| --- | --- |
| `backend/src/evnt/api/` | Tracker, encrypted, health, and proxy HTTP routes |
| `backend/src/evnt/tracker/` | Payload parsing, Iglu validation, IP, User-Agent |
| `backend/src/evnt/storage/clickhouse/` | Client, connector, field registry, DDL |
| `backend/src/evnt/ingest/` | RabbitMQ publisher, request merging, batch worker and its failure handling |
| `backend/src/evnt/middleware/` | ASGI middleware |
| `backend/src/evnt/tests/` | Unit, HTTP, and real-backend integration tests |
| `frontend/` | Vue 3 demo, Vite, Bun |
| `website/` | Docusaurus documentation, Bun |

See [contributing](contributing.md) for the authoritative build and test commands.
