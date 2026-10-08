# Architecture

`evnt` implements the Snowplow tracker wire protocol independently of Snowplow
Analytics Ltd. It receives web/mobile tracker requests, converts them into a
shared row model, and delivers those rows to ClickHouse.

## Request path

1. The middleware applies host/HTTPS rules, CORS, request body limits,
   correlation IDs, logging, and response security headers.
2. `/tracker` binds a batch from JSON; `/i` binds one event from query
   parameters. `/e` unseals the same payload model when encryption is enabled.
3. Shared processing extracts IP and User-Agent data, parses contexts and
   unstructured events, and validates known Iglu schemas.
4. The active row sink writes directly to ClickHouse or publishes to RabbitMQ.

Schema validation is best-effort: a warning or an unknown schema does not make
an otherwise valid tracker request fail. The image requires the Iglu submodule
so known schemas are not silently missing.

## Ingest modes

| Mode | Request acknowledgement | Separate worker |
| --- | --- | --- |
| `direct` | After the ClickHouse insert call succeeds; visibility depends on async-insert settings | No |
| `rabbitmq` | After publishing to the queue; ClickHouse delivery happens later | `evnt queue worker` |

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
| `backend/src/evnt/ingest/` | RabbitMQ publisher and batch worker |
| `backend/src/evnt/middleware/` | ASGI middleware |
| `backend/src/evnt/tests/` | Unit, HTTP, and real-backend integration tests |
| `frontend/` | Vue 3 demo, Vite, Bun |
| `website/` | Docusaurus documentation, Bun |

See [contributing](contributing.md) for the authoritative build and test commands.
