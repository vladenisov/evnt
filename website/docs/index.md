---
slug: /
title: evnt
---

# evnt

`evnt` is a lightweight, self-hosted event collector that **implements the Snowplow tracker wire protocol**. Point any official Snowplow tracker (JS, iOS/Swift, Android/Kotlin, Python, etc.) at `evnt` and it will accept the events, enrich them, and write them to ClickHouse — no hosted Snowplow infrastructure required.

> **Disclaimer.** "Snowplow" is a trademark of Snowplow Analytics Ltd. This is an independent open-source project that interoperates with the publicly documented Snowplow tracker protocol and bundles the official Snowplow JavaScript tracker (BSD-3-Clause) and Iglu Central schemas (Apache-2.0) **unmodified**. It is **not affiliated with, sponsored by, or endorsed by Snowplow Analytics Ltd.** See [THIRD_PARTY_NOTICES.md](license.md) for full attribution.

- **Snowplow-protocol compatible** — receive events from any official tracker without rewriting your client code.
- **ClickHouse-native** — events land in a wide, partitioned `MergeTree` table ready for sub-second analytics.
- **Lean stack** — FastAPI on Python 3.14, async ClickHouse client, no JVM, no Kafka requirement.
- **Optional durable buffer** — flip a flag to switch from direct writes to RabbitMQ + batch worker for high-load or flaky downstreams.
- **Self-hosted, no telemetry** — your data, your infra, your retention policy.
- **Built-in demo UI** — a Vue 3 single-page app at `/demo/` that shows the raw payloads as they leave the browser **and** lets you browse the ClickHouse tables directly from the front-end.

Start with the [quickstart](quickstart.md), connect your [trackers](integrate/trackers.md), or read the [architecture](architecture.md).
