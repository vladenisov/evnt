# HTTP API

Paths below are the defaults. The collector's port inside the image is `8000`;
your reverse proxy determines the public hostname and port.

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| `POST` | `/tracker` | Snowplow JSON batch | `204`, empty body |
| `GET` | `/i` | One event in query parameters | `200`, transparent GIF |
| `OPTIONS` | `/tracker`, `/i` | Collector preflight | `204` without CORS request headers; CORS middleware handles browser preflights |
| `POST` | `/e` | Sealed binary or base64 batch; encryption must be enabled | `204`, empty body |
| `GET` | `/e?d=...` | Base64url sealed payload; encryption must be enabled | `200`, transparent GIF |
| `GET` | `/e.js` | Browser sealer with the current public key | JavaScript, cached for 900 seconds |
| `GET` | `/static/sp/sp.js` | Self-hosted Snowplow JS tracker | JavaScript |
| `GET` | `/live` | Process liveness, no backend check | `204` |
| `GET` | `/` | Active ingest backend readiness | JSON; `503` when unhealthy |
| `GET` | `/metrics` | Prometheus metrics, when enabled | Prometheus text |
| `GET` | `/demo/` | Demo SPA, when enabled | HTML |

The encrypted endpoint and sealer are both absent when encryption is disabled.
Its `OPTIONS` route follows the same CORS behavior as the plaintext routes.
The demo supports history routes under `/demo/` for HTML navigations (`Accept:
text/html` or `*/*`); missing asset files return `404`.
There is no `/health` route.

## Sending a batch

```bash
curl -i http://localhost:8000/tracker \
  -H 'Content-Type: application/json' \
  --data '{"schema":"iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-4","data":[{"e":"pv","aid":"example-app","p":"web","tv":"js-3.0.0","res":"1920x1080"}]}'
```

Invalid plaintext bodies or query parameters return `422` without inserting rows.
If any element in a batch fails request-model validation, the entire batch is
rejected. A storage error returns an error response so a tracker can retry.

All encrypted validation failures return the same `400` response. Payloads over
the configured body/query ceilings return `413`; a missing keyring returns `503`.

## Changing paths

```bash
EVNT_COMMON__SNOWPLOW__ENDPOINTS__POST_ENDPOINT=/tracker
EVNT_COMMON__SNOWPLOW__ENDPOINTS__GET_ENDPOINT=/i
EVNT_ENCRYPTION__ENDPOINT=/e
EVNT_PROMETHEUS__METRICS_PATH=/metrics
```

Keep the paths configured in your trackers and proxies synchronized with these
settings. The sealer URL is always the encrypted endpoint plus `.js`.

## Interactive reference

`EVNT_SECURITY__DISABLE_DOCS=false` enables `/docs`, `/redoc`, and
`/openapi.json` on the collector. These reflect the routes enabled in that
deployment. They are disabled by default; this documentation site is independent
of that setting.

See [tracker setup](trackers.md), [encrypted ingest](encryption.md), and
[security settings](../configure/security.md) for client configuration.
