# Performance

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_PERFORMANCE__USER_AGENT_CACHE_SIZE` | `32768` | Maximum entries in each process-local User-Agent LRU cache. Set to `0` to disable caching. |
| `EVNT_PERFORMANCE__CPU_TASK_CONCURRENCY` | `8` | Maximum CPU-heavy helper calls submitted concurrently by each application process. |
| `EVNT_PERFORMANCE__DB_POOL_SIZE` | `5` | Maximum concurrent ClickHouse HTTP connections per process, both overall and per host. The native async client uses aiohttp rather than a thread-pool wrapper. |

Those offloads run on asyncio's default thread pool, which holds
`min(32, cpu_count + 4)` workers. Raising `CPU_TASK_CONCURRENCY` above that
number has no effect: the pool, not the limiter, becomes the bottleneck.

`/live` is a lightweight process liveness endpoint and does not query the active
ingest backend. The existing `/` probe remains the backend readiness check.
It is excluded from the access log by default via
`EVNT_PERFORMANCE__ACCESS_LOG_EXCLUDED_PATHS`; override that list to change it.
