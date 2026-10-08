# Analytics-script proxy

The optional proxy at `/proxy` fetches allowlisted third-party analytics scripts so you can serve them first-party. It is constrained to prevent SSRF:

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_PROXY__DOMAINS` | `["google-analytics.com", "www.googletagmanager.com"]` | Hostname allowlist: `/proxy/route` fetches only from these hosts, and `/proxy/hash` rewrites only their URLs. |
| `EVNT_PROXY__ALLOWED_PORTS` | `[80, 443]` | Outbound ports the proxy may reach on an allowlisted host. A target with no explicit port (the scheme default) is always permitted; any other port is rejected with `403`. |

Redirects are **not** followed, so an allowlisted host cannot bounce the proxy to an internal target.
