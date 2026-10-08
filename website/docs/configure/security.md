# Security

The most important settings to know about:

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_SECURITY__DISABLE_DOCS` | `true` | `/docs`, `/redoc` and `/openapi.json` are **disabled**. Set to `false` to expose them. |
| `EVNT_SECURITY__CORS_ALLOWED_ORIGINS` | `["*"]` | JSON array of bare HTTP(S) origins (e.g. `["https://example.com"]`), or `["*"]`. |
| `EVNT_SECURITY__CORS_ALLOW_CREDENTIALS` | `true` | Credentialed CORS responses include `Access-Control-Allow-Credentials: true`. |
| `EVNT_SECURITY__TRUSTED_HOSTS` | `["*"]` | Allowed `Host` header values. |
| `EVNT_SECURITY__TRUST_PROXY_HEADERS` | `true` | When enabled, the client IP is taken from the configured proxy header (`X-Forwarded-For` by default). Set to `false` if `evnt` is exposed directly (no trusted reverse proxy) so clients cannot spoof their IP. |
| `EVNT_SECURITY__ENABLE_HTTPS_REDIRECT` | `false` | Adds an HSTS header and HTTPS redirect when enabled. |
| `EVNT_SECURITY__MAX_REQUEST_BODY_BYTES` | `10485760` | Ceiling on any request body, enforced before the handler runs. Refused with `413`. The encrypted endpoint applies its own, much tighter limit on top. |

**Re-enabling the API docs.** Interactive docs are off by default. To turn them back on (e.g. for a private/staging instance):

```bash
EVNT_SECURITY__DISABLE_DOCS=false
```

**CORS with credentials.** Browser credentials (cookies, `Authorization`) are allowed by default for collector compatibility. The default wildcard origin setting reflects the request `Origin` instead of returning `Access-Control-Allow-Origin: *`, so browser requests using `credentials: "include"` are accepted.

To restrict credentialed cross-origin requests to known frontends, list explicit origins:

```bash
EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["https://app.example.com"]'
EVNT_SECURITY__CORS_ALLOW_CREDENTIALS=true
```

To disable credentialed CORS while still accepting requests from any origin:

```bash
EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["*"]'
EVNT_SECURITY__CORS_ALLOW_CREDENTIALS=false
```
