# Docker deployment

```bash
docker run -d -p 8000:8000 \
  -e EVNT_CLICKHOUSE__CONNECTION__HOST=clickhouse.internal \
  -e EVNT_CLICKHOUSE__CONNECTION__PASSWORD=... \
  vladenisov/evnt:latest
docker run --rm -e EVNT_CLICKHOUSE__CONNECTION__HOST=... vladenisov/evnt evnt db init
```

The container runs as the unprivileged `evnt` user (uid 1000) and listens on
**port 8000**. Its `HEALTHCHECK` polls `/live`; point load-balancer readiness
checks at `/`, which also checks the ingest backend. Every `evnt` CLI command
is available inside the image (`docker run --rm vladenisov/evnt evnt settings`).

## Runtime compatibility

The image ships the installed `evnt` and `uvicorn` executables. It does not
ship `uv`, `curl`, a root-level `main.py`, or a root-level `cli.py`.

For a command override, use:

```bash
uvicorn evnt.main:app --host 0.0.0.0 --port 8000 --workers 6
```

Publish your existing external port to container port `8000`. An nginx or
Varnish backend on the Docker network should also use `8000`. Public tracker
and asset paths stay the same.

An HTTP healthcheck can use the Python standard library available in the image:

```bash
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/live', timeout=3).close()"
```

Mount secrets and set connection variables for your environment. Run `evnt db
init` as an explicit setup/migration step; starting the API does not create
tables. In [RabbitMQ mode](queue.md), start a separate worker process as well.
