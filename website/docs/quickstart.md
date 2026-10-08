# Quickstart

```bash
git clone https://github.com/vladenisov/evnt.git
cd evnt

git submodule update --init --depth 1   # Iglu schemas; the image build requires them

# 1. Bring up ClickHouse first (the app waits for it).
docker compose up -d clickhouse

# 2. One-time: create the `evnt` database and tables (idempotent).
docker compose run --rm app evnt db init

# 3. Start the collector.
docker compose up -d
#    Or with the RabbitMQ buffer: the profile only adds the services,
#    the ingest mode has to be switched as well.
# EVNT_INGEST__MODE=rabbitmq docker compose --profile rabbitmq up -d
```

For development (watch mode, tests, linters) see [contributing](contributing.md);
`make` lists every shortcut.



Open <http://localhost:8000/demo/> to try the [demo](run/demo.md). For an existing ClickHouse, see [Docker deployment](run/docker.md).
