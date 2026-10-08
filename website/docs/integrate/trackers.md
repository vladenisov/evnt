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
