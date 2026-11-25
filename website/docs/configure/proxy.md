# Analytics-script proxy

The optional proxy at `/proxy` fetches allowlisted third-party analytics scripts so you can serve them first-party. It is constrained to prevent SSRF:

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_PROXY__DOMAINS` | `["google-analytics.com", "www.googletagmanager.com"]` | Hostname allowlist: `/proxy/route` fetches only from these hosts, and `/proxy/hash` rewrites only their URLs. |
| `EVNT_PROXY__ALLOWED_PORTS` | `[80, 443]` | Outbound ports the proxy may reach on an allowlisted host. A target with no explicit port (the scheme default) is always permitted; any other port is rejected with `403`. |

Redirects are **not** followed, so an allowlisted host cannot bounce the proxy to an internal target.

## Public origin of rewritten URLs

`POST /proxy/hash` answers with a URL on the collector's own origin. Where that origin comes from:

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_COMMON__HOSTNAME` | unset | Public origin of the collector, for example `https://collector.example.com`. When set, every rewritten URL uses exactly this scheme, host and port. |

With `EVNT_COMMON__HOSTNAME` unset, the origin is taken from the request that asked for the hash:

- the host and port come from the `Host` header, which `EVNT_SECURITY__TRUSTED_HOSTS` restricts when configured. With the default `["*"]` the returned origin simply mirrors the caller's `Host`. `X-Forwarded-Host` is never read;
- the scheme comes from `X-Forwarded-Proto` when `EVNT_SECURITY__TRUST_PROXY_HEADERS=true` (the default) and the header says `http` or `https`; otherwise it is the scheme of the connection itself.

Set `EVNT_COMMON__HOSTNAME` when the reverse proxy does not pass the public `Host` through (nginx replaces it with the upstream name unless told `proxy_set_header Host $host`), or does not send `X-Forwarded-Proto` while terminating TLS. `evnt settings hostname` prints the configured value, which is `http://localhost:8000/` when unset.

A `Host` header that is missing or malformed cannot be used as an origin: the hash then falls back to `http://localhost:8000` and the collector logs a warning naming the header.

## Caching

`/proxy/route` relays the upstream `Cache-Control`, `ETag`, `Last-Modified` and `Expires` headers and passes the browser's `If-None-Match` and `If-Modified-Since` upstream, so a cached script revalidates with a bodyless `304` instead of being downloaded again. No other request or response header crosses the proxy, apart from `Content-Type`.
