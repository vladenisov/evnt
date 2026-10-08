# evnt

A lightweight self-hosted event collector that speaks the Snowplow tracker
wire protocol and writes to ClickHouse, directly or through RabbitMQ. Optional
encrypted ingestion, first-party analytics scripts, and a Vue 3 demo.

Independent of Snowplow Analytics Ltd. Licensed under BSD 3-Clause.

## Quickstart

```bash
git clone https://github.com/vladenisov/evnt.git
cd evnt
git submodule update --init --depth 1
docker compose up -d clickhouse
docker compose run --rm app evnt db init
docker compose up -d
```

Open <http://localhost:8000/demo/>. For the queue buffer:

```bash
EVNT_INGEST__MODE=rabbitmq docker compose --profile rabbitmq up -d
```

The image runs as a non-root user on port **8000**. `/live` is liveness;
`/` is backend readiness. Tables are created explicitly by `evnt db init`.
Tracker and sealer URLs remain `/static/sp/sp.js` and `/e.js` (encryption opt-in).

## Documentation

Documentation sources are in [`website/docs`](website/docs), built with
Docusaurus and Bun:

```bash
make install-docs
make dev-docs
```

- [Quickstart](website/docs/quickstart.md)
- [Trackers and first-party assets](website/docs/integrate/trackers.md)
- [HTTP API](website/docs/integrate/http-api.md)
- [Encrypted ingest](website/docs/integrate/encryption.md)
- [Configuration](website/docs/configure/settings.md) and [security](website/docs/configure/security.md)
- [Docker deployment](website/docs/run/docker.md) and [RabbitMQ worker](website/docs/run/queue.md)
- [Architecture](website/docs/architecture.md)
- [Development and testing](website/docs/contributing.md)
- [Publish documentation on GitHub Pages](website/docs/run/documentation.md)
- [Publish releases](website/docs/run/releases.md)
- [Releases](https://github.com/vladenisov/evnt/releases)

Settings use the `EVNT_` prefix and `__` nesting. See [`.env.example`](.env.example)
for common variables, or run `cd backend && uv run evnt settings` for defaults.
The full configuration reference lives in the documentation above.

## License and attribution

See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Downstream images bundling tracker scripts or Iglu schemas must retain the
third-party notices. “Snowplow” is a trademark of Snowplow Analytics Ltd;
this project is not affiliated with, sponsored by, or endorsed by it.
