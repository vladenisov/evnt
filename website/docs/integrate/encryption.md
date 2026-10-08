# Encrypted ingest

`/e` is the sealed-payload twin of `/tracker`. Clients encrypt the **same** Snowplow JSON body with the collector's public key; only the collector holds the private key. Nothing else changes — the payload model, parsing, and ClickHouse rows are identical.

The endpoint is off by default and is only mounted when enabled, so deployments that do not use it expose no extra surface.

```bash
uv sync --extra crypto          # from backend/; cryptography is optional
uv run evnt keys generate --kid=k1
```

That prints a public key to embed in your clients and a private key for the collector:

```bash
EVNT_ENCRYPTION__ENABLED=true
EVNT_ENCRYPTION__KEYS=[{"kid":"k1","private_key":"<base64>"}]
```

| Setting | Default | Notes |
| --- | --- | --- |
| `EVNT_ENCRYPTION__ENABLED` | `false` | Mounts `/e` when true. |
| `EVNT_ENCRYPTION__ENDPOINT` | `/e` | Serves `POST`, `GET`, and `OPTIONS`. |
| `EVNT_ENCRYPTION__KEYS` | `[]` | List of `{kid, private_key}` or `{kid, private_key_file}`, plus optional `enabled`. |
| `EVNT_ENCRYPTION__MAX_ENVELOPE_BYTES` | `262144` | POST bodies above this get `413`. Capped at 8 MiB. |
| `EVNT_ENCRYPTION__MAX_PLAINTEXT_BYTES` | `1048576` | Ceiling after decompression, so a compression bomb cannot exhaust memory. Capped at 64 MiB, and at 16x the envelope ceiling. |
| `EVNT_ENCRYPTION__MAX_QUERY_BYTES` | `8192` | Separate, much smaller ceiling for the `GET` form, which every proxy caps near 8 KB anyway. |

Private keys are `SecretStr` and stay out of config dumps and logs. Prefer `private_key_file` to mount the key as a secret rather than exposing it in the process environment.

#### Scheme

Sealed box: **ephemeral X25519 → HKDF-SHA256 → AES-256-GCM**.

- **X25519** is native on every target — iOS CryptoKit (13+), Android via Tink or Conscrypt, Web via WebCrypto or `@noble/curves` (~8 KB). Fixed 32-byte keys, no ASN.1, no curve-point validation.
- **AES-256-GCM** is the only AEAD WebCrypto exposes natively, and is hardware-accelerated on current phones.
- **A fresh ephemeral key per event** gives a per-event content key, so a nonce can never be reused. A static client key would buy no authentication anyway — it would ship inside the app bundle.

Envelope, `67 + len(kid)` bytes of overhead, sent raw as `application/octet-stream` or base64 as text:

```
offset  size  field
0       4     magic      "EVN1"
4       1     version    0x01
5       1     flags      bit0 = plaintext deflated before sealing
                         (gzip or zlib container — both accepted)
6       1     kid_len    1..32
7       N     kid        ASCII key id
7+N     32    epk        ephemeral X25519 public key
39+N    12    nonce      AES-GCM nonce
51+N    ..    ct         ciphertext || 16-byte tag

shared = X25519(ephemeral_secret, recipient_public)
key    = HKDF-SHA256(ikm=shared, salt="", info="evnt/e/v1" || epk || recipient_public, 32)
aad    = envelope[0 : 39+N]
```

`backend/src/evnt/crypto.py::seal_envelope` is the executable specification — a client implementation is correct exactly when it produces envelopes that function would produce. It emits gzip, but the collector auto-detects the container, so a client using Android's `Deflater` (zlib) rather than `GZIPOutputStream` (gzip) interoperates without changes.

Unsealing runs inline on the event loop: a typical batch measures ~0.04 ms and the 1 MiB ceiling ~0.7 ms, which is why the ceilings above are what bound per-request cost. Raising them raises that cost proportionally.

The `kid` travels in cleartext, so several key pairs stay live at once and rotation needs no client flag day: add the new key, ship clients that use it, then drop the old one.

#### Client wiring

Mobile trackers hook in by replacing the network layer — Snowplow's [`NetworkConnection`](https://docs.snowplow.io/docs/sources/mobile-trackers/configuring-how-events-are-sent/?platform=android#configuring-the-network-connection) — so the tracker still builds ordinary Snowplow payloads and only the transport changes. Seal the request body, POST it to `/e` as `application/octet-stream`, and treat `204` as success. `GET /e?d=<base64url envelope>` returns a tracking pixel for transports that cannot POST; query-string limits make it suitable for single events only.

#### What this does and does not protect

It keeps payloads unreadable to anything between the client and the collector, including a TLS-terminating proxy or an on-device interceptor reading plaintext traffic.

It is **not** client authentication and **not** replay protection: the public key ships inside the app, so anyone who extracts it can seal valid payloads, and a captured envelope can be resent. Deduplicate downstream on `event_id` if that matters. Every rejection — bad key id, failed tag, malformed JSON, schema violation — returns the same opaque `400`, so the endpoint cannot be used as an oracle; the real reason is in the server log.
