# Trackers and first-party assets

`evnt` speaks the Snowplow tracker protocol. Use the official trackers — point their collector URL at `https://your-evnt-host` and they’ll just work. Reference docs:

- **JavaScript** (web) — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/javascript-trackers/>
- **Swift / iOS** — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/mobile-trackers/>
- **Kotlin / Android** — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/mobile-trackers/>
- **Python** — <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/python-tracker/>

The full tracker matrix (Java, Go, .NET, Roku, Unity, Lua, …) is at <https://docs.snowplow.io/docs/collecting-data/collecting-from-own-applications/>.

The Docker image already ships the official Snowplow JS bundle, served
first-party at `/static/sp/sp.js`. Outside the image, fetch it with:

```bash
cd backend && uv run evnt scripts download
```

That places `sp.js` (and plugins) into `backend/static/sp/` (the
`EVNT_COMMON__STATIC_DIR` directory, `/app/static` in the image). The vendored
subtree is gitignored and regenerated on every image build.

## Public asset URLs

Existing tags can continue loading the tracker from `/static/sp/sp.js` and the
browser sealer from `/e.js`. The collector's internal directory layout does not
change these URLs. `/e.js` requires encryption to be enabled and keys to be loaded.

The sealer exposes `evnt.encryptedFetch` for a tracker's custom transport. Load
the sealer before configuring that transport and keep the standard tracker
transport available if the script fails to load or the browser lacks X25519.
See [encrypted ingest](encryption.md) for the wire format and key configuration.

## Tracker compatibility

The collector accepts what the official trackers send even where it departs
from the strictest reading of the protocol:

- `aid` is optional (the JavaScript tracker omits it when `appId` is empty) and
  is stored as an empty string.
- `res` is optional: visionOS, and macOS without a main screen, never send it.
- `p=headset` (visionOS and other headsets, from the iOS and Android trackers)
  is stored as `mob`, because the `platform` column has no `headset` value; the
  code as sent is kept in `extra.tracker.platform`. An unknown platform code, or
  a missing `p`, is stored as `app` (Snowplow's generic application code), with
  the code kept the same way and an `extra.validation` entry.
- `duid` and `sid` that are not UUIDs (old or custom cookies) are stored as
  empty, with the rejected value quoted in `extra.validation`.
- The nil UUID `00000000-0000-0000-0000-000000000000`, which the mobile
  trackers send for an anonymised user (`userAnonymisation`), counts as no id,
  in `duid`, `sid` and the `client_session` context (`userId`, `sessionId`,
  `previousSessionId`). It never replaces a real id. `device_id` is not
  nullable, so an event without a device id still stores the zero UUID there.
- `ttm` (true timestamp) is read like `dtm`. The JavaScript tracker sends `ttm`
  instead of `dtm` when the caller supplies the event time, so when `dtm` is
  missing or invalid, `ttm` becomes the event `time`. It is always kept in
  `extra.tracker.true_tstamp`.
- A mobile `screen_view` stores the screen name in `page` (the URL column) and
  the previous screen name in `referer`, as mobile dashboards expect. When the
  tracker had sent its own `url` or `refr` (the iOS and Android trackers copy a
  deep link there for campaign attribution), the originals are kept in
  `extra.tracker.url` and `extra.tracker.refr`. A `screen_view` without `id` or
  `name` is stored rather than failing the event.
- `ua` (User-Agent override, sent by server-side trackers and WebView bridges)
  is parsed instead of the `User-Agent` header, for any platform: the
  `user_agent` column and the parsed browser, OS and device fields come from
  it. The value is also kept in `extra.tracker.ua`. Longer than 2048
  characters, it is ignored and listed in `extra.validation`. One request has
  at most 32 distinct `ua` values parsed, which bounds the parsing cost of a
  batch; past that, a value the collector has already parsed for an earlier
  request is still used, and any other one falls back to the `User-Agent`
  header with an `extra.validation` entry (`field` is `ua`).
- `ip` (client IP override) replaces the connection address in `user_ip` only
  for server-side events (`p=srv`); a browser or app cannot move its own events
  to another address. It goes through the same IPv4 mapping as a connection
  address (native IPv6 is stored as `0.0.0.0`). An `ip` that is not an IP
  address leaves `user_ip` alone and is listed in `extra.validation`. Whatever
  the platform, the value as sent is kept in `extra.tracker.ip`, and
  `extra.collector.client_ip` stays the address of the connection: an analyst
  sees both what the collector saw and what the event was attributed to. A
  request whose `SP-Anonymous` header the collector honours ignores `ip`
  entirely; see [server anonymisation](../configure/settings.md#server-anonymisation-sp-anonymous).
- `tnuid` (network user id set by the tracker) is kept in
  `extra.tracker.network_userid`.
- An empty `ua`, `ip` or `tnuid` counts as not sent. `ip` and `tnuid` longer
  than 64 and 128 characters are ignored and listed in `extra.validation`.

## What the collector stores in `extra`

Besides the context data it has always merged into the `extra` JSON column,
the collector owns these keys. They are written only when there is something
to store, and a payload cannot set them. ClickHouse reads a dot in a JSON key
as nesting, so a `com.acme/static_context` key such as `tracker.ip` would be the
path `extra.tracker.ip`: a static context key whose first dotted segment is
`validation`, `tracker`, `contexts`, `event_fingerprint` or `collector` is not
merged, and one `extra.validation` entry (stage `contexts`) names the dropped
keys. A repeated static context kept in `extra.contexts` keeps them, as text.

| Key | Content |
| --- | --- |
| `extra.validation` | Problems of an event that was still stored, in the order found: a list of objects with `stage`, `field` or `schema`, and `error`. At most 20 entries, each `error` cut to 200 characters. |
| `extra.tracker` | Tracker-sent values the row does not carry as sent: `platform`, `true_tstamp` (ISO 8601 in UTC, for example `2023-11-14T22:13:20.123Z`; ClickHouse may infer it as `DateTime64`), `url`, `refr`, `ua`, `ip`, `network_userid` (the `tnuid` parameter). Only the keys that apply are present. |
| `extra.contexts` | Context entities the collector has no column mapping for, and repeats of one it has; see [context entities](#context-entities). |
| `extra.event_fingerprint` | On every event: 32 hex characters identifying the event as its tracker sent it, for finding resent copies; see [event fingerprint](#event-fingerprint). |
| `extra.collector` | `client_ip` and `ip_country`, and `geo` when [visitor location](../configure/settings.md#visitor-location-cloudflare) is on; only `anonymous: true` for a request whose [`SP-Anonymous`](../configure/settings.md#server-anonymisation-sp-anonymous) header is honoured. See [client network metadata](../configure/settings.md#client-network-metadata). |

`stage` is one of:

- `payload`: an atomic tracker parameter (`field` names it, the error quotes the
  rejected value); the field was stored with its default. An entry whose error
  is `unpaired surrogate replaced with U+FFFD` is the exception: the value was
  kept, with each half of a broken surrogate pair (a truncated emoji) replaced
  by U+FFFD, and `field` names the stored column.
- `contexts`: a context entity that is malformed, whose schema is not an Iglu
  URI, or whose data failed its Iglu schema (`schema` holds the URI); also one
  entry for the entities over the [`extra.contexts` limits](#context-entities).
- `event`: the self-describing event (`ue_pr`/`ue_px`) is malformed or its data
  failed its schema; malformed event data is not stored in `unstructured`.
- `processing`: parsing raised; only the validated atomic fields were stored.

A schema the collector cannot load (for example a custom vendor missing from
the Iglu schemas) is not an error of the event and is only logged.

```sql
SELECT
    event_id,
    arrayMap(e -> (e.stage.:String, e.field.:String, e.error.:String),
             extra.validation.:`Array(JSON)`) AS problems
FROM evnt.local
WHERE time >= now() - INTERVAL 1 DAY AND extra.validation IS NOT NULL
LIMIT 20;
```

## Context entities

The collector maps the entities it knows (web page, client session, browser,
mobile, screen, application and others) into columns. Every other entity is
kept in `extra.contexts`, under its `vendor/name`, in the order received:

```json
{
  "contexts": {
    "com.snowplowanalytics.snowplow.ecommerce/product": [
      {"version": "1-0-0", "data": "{\"id\":\"sku-1\",\"price\":9.99}"},
      {"version": "1-0-0", "data": "{\"id\":\"sku-2\",\"price\":5}"}
    ]
  }
}
```

- `version` is the schema version (`MODEL-REVISION-ADDITION`); `data` is the
  entity's data as JSON text. It is text, not a nested object, because
  arbitrary tracker JSON can make the ClickHouse `JSON` column reject the whole
  insert (a key `a.b` next to an object `a` with a key `b` is a duplicate
  path), and nested objects would add a subcolumn for every key of every
  schema. Read it with `JSONExtract*` or a cast to `JSON`.
- A schema the collector maps is kept here too when an event carries it more
  than once, since the columns merge or overwrite repeats: every occurrence
  fills the columns as before, and the second and later ones are also kept
  here whole.
- Data that failed its Iglu schema is kept all the same, with an entry in
  `extra.validation`. An entity whose schema is not an Iglu URI
  (`iglu:vendor/name/format/1-0-0`) has no key to go under and is only listed
  in `extra.validation`.
- At most 256 entities and 64 KiB of `data` per event are kept. The trackers
  cap a whole request at 40 000 bytes by default, so real events stay well
  below; entities over the limits are dropped and counted in one
  `extra.validation` entry.

ClickHouse splits JSON keys at dots, so a vendor with dots becomes nested
paths: the whole key is still one path in a query (quote it with backticks),
but `SELECT extra` shows `{"contexts": {"com": {"snowplowanalytics": ...}}}`.

```sql
SELECT
    event_id,
    arrayMap(e -> (e.version.:String, JSONExtractString(e.data.:String, 'id')),
             extra.contexts.`com.snowplowanalytics.snowplow.ecommerce/product`.:`Array(JSON)`)
        AS products
FROM evnt.local
WHERE time >= now() - INTERVAL 1 DAY
  AND extra.contexts.`com.snowplowanalytics.snowplow.ecommerce/product` IS NOT NULL
LIMIT 20;
```

## Event fingerprint

A tracker resends a batch it got no success response for, so one event can be
stored twice, for example when the response was lost on the way back.
`extra.event_fingerprint` lets those copies be found: a BLAKE2b hash (16
bytes, hex) of the event's parameters exactly as received, leaving out the ones
that change between sends, as Snowplow's own `event_fingerprint` does: `eid`,
`stm` (send time), `nuid` and `cv`. Every send of one event has the same
fingerprint; any other difference, including in the contexts, gives another.
The collector does not deduplicate; the tables are unchanged.

```sql
SELECT extra.event_fingerprint.:String AS fingerprint, count() AS copies
FROM evnt.local
WHERE time >= now() - INTERVAL 1 DAY
GROUP BY fingerprint
HAVING copies > 1
ORDER BY copies DESC
LIMIT 20;
```
