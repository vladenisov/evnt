# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Security
- **CORS collector default**: `EVNT_SECURITY__CORS_ALLOW_CREDENTIALS` defaults to `true` with `EVNT_SECURITY__CORS_ALLOWED_ORIGINS='["*"]'`, so tracker requests using browser credentials work out of the box. Wildcard origins are implemented by reflecting the request `Origin`, not by returning `Access-Control-Allow-Origin: *`.
- **API docs disabled by default**: `EVNT_SECURITY__DISABLE_DOCS` now defaults to `true`, so `/docs`, `/redoc` and `/openapi.json` are off. Set it to `false` to re-enable.
- **Proxy SSRF fixes**: the `/proxy` endpoint no longer auto-follows redirects (an allowlisted host could otherwise bounce it to an internal target), and a new `EVNT_PROXY__ALLOWED_PORTS` allowlist (default `[80, 443]`) restricts outbound ports so an attacker cannot append a port to reach internal services.
- **Secrets redacted**: ClickHouse and RabbitMQ passwords are now `SecretStr`; their values are kept out of config dumps and logs while remaining env-configurable.
- **Proxy-header trust enforced**: `EVNT_SECURITY__TRUST_PROXY_HEADERS` is now actually honored for client-IP extraction. Set it to `false` when `evnt` is exposed directly so clients cannot spoof their IP via `X-Forwarded-For`.
- Dropped the deprecated `X-XSS-Protection` response header.

### Reliability
- Offloaded blocking work (jsonschema validation, user-agent parsing) to worker threads so per-request CPU work no longer blocks the event loop.
- **RabbitMQ worker**: clean `SIGTERM` shutdown (final flush + close), capped exponential backoff moved out of the flush path into the run loop, publisher confirms on failed-queue publishes to prevent silent loss, and a liveness file consumed by a new `evnt queue healthcheck` (wired as the worker container `HEALTHCHECK` in `compose.yml`).
- ClickHouse startup connection retry in the worker, matching the app lifespan.
- Guarded untrusted UUID/datetime/dict-key access in the payload parser to avoid uncaught 500s.

### Changed
- `EVNT_SECURITY__DISABLE_DOCS` default `false` → `true`.
- `EVNT_SECURITY__CORS_ALLOW_CREDENTIALS` defaults to `true` for collector compatibility.
- ClickHouse/RabbitMQ password fields changed type to `SecretStr`.

### Added
- `EVNT_PROXY__ALLOWED_PORTS` (default `[80, 443]`) — outbound proxy port allowlist.
- `evnt queue healthcheck` CLI command — liveness-aware health probe for the RabbitMQ worker.

## [v2.2.0] - 2025-05-28

### Changed
- **BREAKING**: Transformed all Nullable columns to non-nullable with default values
- Updated data migration to use `coalesce()` function for handling NULL values
- Set default UUID value for session fields when NULL

### Migration Instructions

Run the following SQL to migrate from the old schema to the new non-nullable schema:

```sql
INSERT INTO snowplow.new (app_id, platform, app_info, page, referer, event_type, event_id, view_id, session_id, visit_count, session, amp, device_id, user_id, time, time_extra, timezone, title, screen, page_data, user_data, user_ip, geolocation, user_agent, browser, os, device, device_is, resolution, event, extra, tracker) SELECT
    app_id,
    platform,
    (app_info.version, app_info.build),
    page,
    referer,
    event_type,
    event_id,
    view_id,
    session_id,
    coalesce(visit_count, 0),
    (
        coalesce(session.event_index, 0),
        coalesce(session.previous_session_id, toUUID('00000000-0000-0000-0000-000000000000')),
        coalesce(session.first_event_id, toUUID('00000000-0000-0000-0000-000000000000')),
        coalesce(session.first_event_time, toDateTime64('1970-01-01', 3)),
        session.storage_mechanism,
        session.unstructured
    ),
    amp,
    device_id,
    user_id,
    time,
    (time_extra.received, time_extra.sent),
    timezone,
    title,
    screen,
    page_data,
    user_data,
    user_ip,
    geolocation,
    user_agent,
    (
        browser.family,
        browser.version,
        browser.extra
    ),
    (os.family, os.version, os.language),
    (
        device.brand,
        device.model,
        device.extra
    ),
    (device_is.mobile, device_is.tablet, device_is.touch, device_is.pc, device_is.bot),
    (resolution.browser, resolution.viewport, resolution.page),
    (event.action, event.category, event.label, event.property, event.value, event.unstructured),
    extra,
    (tracker.version, tracker.namespace)
FROM snowplow.old;
```

---

## [v2.1.0] - 2024-12-27

### Added
- JSON column type support for flexible data structures
- Data validation using `isValidJSON()` function before insertion
- Improved data handling for AMP, screen, and device information

### Changed
- Replaced tuple structures with JSON columns for better flexibility
- Enhanced data transformation logic for complex nested structures
- Improved string escaping and data sanitization

### Migration Instructions

Use this SQL to migrate to the new JSON-based schema:

```sql
INSERT INTO snowplow.new (app_id, platform, app_info, page, referer, event_type, event_id, view_id, session_id, visit_count, session, amp, device_id, user_id, time, time_extra, timezone, title, screen, page_data, user_data, user_ip, geolocation, user_agent, browser, os, device, device_is, resolution, event, extra, tracker) SELECT
    app_id,
    platform,
    (app.version, app.build),
    page,
    referer,
    event_type,
    event_id,
    view_id,
    session_id,
    visit_count,
    (session.event_index, session.previous_session_id, session.first_event_id, session.first_event_time, session.storage_mechanism, session.unstructured),
    if(
        amp.client_id = '', '{}'::JSON,
        ('{"domainUserid":"' || amp.device_id || '",' ||
        '"ampClientId":"' || amp.client_id || '",' ||
        '"ampSessionId":' || toString(amp.session_id) || ','  ||
        '"ampSessionIndex":' || toString(amp.visit_count) || ',' ||
        '"sessionEngaged":' || if(amp.session_engaged, 'true', 'false') || ',' ||
        '"sessionCreationTimestamp":' ||
            if(amp.first_event_time IS NULL, 'null', '"' || toString(amp.first_event_time) || '"') || ',' ||
        '"lastSessionEventTimestamp":' ||
            if(amp.previous_session_time IS NULL, 'null', '"' || toString(amp.previous_session_time) || '"') || ',' ||
        '"ampPageViewId":"' || amp.view_id || '"}')::JSON
    ) AS new_amp,
    device_id,
    user_id,
    time,
    (time_extra.received, time_extra.sent),
    timezone,
    title,
    ('{' ||
    if(screen.type != '', '"type":"' || screen.type || '",', '') ||
    if(screen.view_controller != '', '"viewController":"' || screen.view_controller || '",', '') ||
    if(screen.top_view_controller != '', '"topViewController":"' || screen.top_view_controller || '",', '') ||
    if(screen.activity != '', '"activity":"' || screen.activity || '",', '') ||
    if(screen.fragment != '', '"fragment":"' || screen.fragment || '",', '') ||
    if(screen.unstructured.previous_id is not null, '"previousId":"' || screen.unstructured.previous_id || '"', '') ||
    '}')::JSON AS new_screen,
    page_data,
    user_data,
    user_ip,
    geolocation,
    user_agent,
    (
        browser.family,
        browser.version,
        if(browser.family != '',
            ('{' ||
            if(browser.unstructured.deviceMemory is not null, '"deviceMemory":' || toString(browser.unstructured.deviceMemory) || ',', '') ||
            if(browser.unstructured.devicePixelRatio is not null, '"devicePixelRatio":' || toString(browser.unstructured.devicePixelRatio) || ',', '') ||
            if(browser.unstructured.documentLanguage is not null, '"documentLanguage":"' || browser.unstructured.documentLanguage || '",', '') ||
            if(browser.unstructured.hardwareConcurrency is not null, '"hardwareConcurrency":' || toString(browser.unstructured.hardwareConcurrency) || ',', '') ||
            if(browser.unstructured.online is not null, '"online":' || toString(browser.unstructured.online) || ',', '') ||
            if(browser.unstructured.tabId is not null, '"tabId":"' || browser.unstructured.tabId || '",', '') ||
            if(browser.unstructured.webdriver is not null, '"webdriver":' || toString(browser.unstructured.webdriver) || ',', '') ||
            if(browser.color_depth != 0, '"colorDepth":' || toString(browser.color_depth) || ',', '') ||
            if(browser.charset != '', '"charset":"' || browser.charset || '",', '') ||
            '"cookiesEnabled":' || toString(browser.cookie) || '}')::JSON
            , '{}'::JSON
        )
    ),
    (os.family, os.version, os.language),
    (
        device.brand,
        device.model,
        if(device_extra.network_type = '' AND device_extra.carrier = '', '{}',
	        ('{' ||
	        if(device_extra.carrier != '', '"carrier":"' || replaceAll(replaceAll(replaceAll(replaceAll(device_extra.carrier, '\r', '\\r'), '\n', '\\n'), '\x11', ' '), '\"', '\\"') || '",', '') ||
	        if(device_extra.network_type != '', '"networkType":"' || device_extra.network_type || '",', '') ||
	        if(device_extra.network_technology != '', '"networkTechnology":"' || device_extra.network_technology || '",', '') ||
	        if(device_extra.open_idfa != '', '"openIdfa":"' || device_extra.open_idfa || '",', '') ||
	        if(device_extra.apple_idfa != '', '"appleIdfa":"' || device_extra.apple_idfa || '",', '') ||
	        if(device_extra.apple_idfv != '', '"appleIdfv":"' || device_extra.apple_idfv || '",', '') ||
	        if(device_extra.android_idfa != '', '"androidIdfa":"' || device_extra.android_idfa || '",', '') ||
	        '"batteryLevel":' || toString(device_extra.battery_level) || ',' ||
	        '"batteryState":"' || device_extra.battery_state || '",' ||
	        '"lowPowerMode":' || if(device_extra.low_power_mode, 'true', 'false') || '}')
	    )::JSON
    ),
    (device_is.mobile, device_is.tablet, device_is.touch, device_is.pc, device_is.bot),
    (resolution.browser, resolution.viewport, resolution.page),
    (event.action, event.category, event.label, event.property, event.value, event.unstructured),
    extra,
    (tracker.version, tracker.namespace)
FROM snowplow.old;
```

---

## [v2.0.0] - 2024-11-15

### Added
- Experimental JSON column type support
- Enhanced data validation with `isValidJSON()` function

### Changed
- **BREAKING**: Minimum ClickHouse version requirement updated to `24.11`
- **BREAKING**: Schema changes require new table creation (migration not possible)
- Improved data structure handling with JSON validation

### Migration Instructions

⚠️ **Important**: Migration of existing tables is not possible due to breaking schema changes.

Create a new table and migrate data using:

```sql
INSERT INTO snowplow.new (app_id, platform, app, page, referer, event_type, event_id, view_id, session_id, visit_count, session, amp, device_id, user_id, time, time_extra, timezone, title, screen, page_data, user_data, user_ip, geolocation, user_agent, browser, os, device, device_is, device_extra, resolution, event, extra, tracker) SELECT
    app_id,
    platform,
    (app.version, app.build),
    page,
    referer,
    event_type,
    event_id,
    view_id,
    session_id,
    visit_count,
    (session.event_index, session.previous_session_id, session.first_event_id, session.first_event_time, session.storage_mechanism, if(isValidJSON(session.unstructured), session.unstructured, '{}')),
    (amp.device_id, amp.client_id, amp.session_id, amp.visit_count, amp.session_engaged, amp.first_event_time, amp.previous_session_time, amp.view_id),
    device_id,
    user_id,
    time_extra.user,
    (time, time_extra.sent),
    timezone,
    title,
    (screen.type, screen.view_controller, screen.top_view_controller, screen.activity, screen.fragment, if(isValidJSON(screen.unstructured), screen.unstructured, '{}')),
    if(isValidJSON(page_data), page_data, '{}'),
    if(isValidJSON(user_data), user_data, '{}'),
    user_ip,
    if(isValidJSON(geolocation), geolocation, '{}'),
    user_agent,
    (browser.family, browser.version, browser.cookie, browser.charset, browser.color_depth, if(isValidJSON(browser.unstructured), browser.unstructured, '{}')),
    (os.family, os.version, os.language),
    (device.brand, device.model),
    (device_is.mobile, device_is.tablet, device_is.touch, device_is.pc, device_is.bot),
    (device_extra.carrier, device_extra.network_type, device_extra.network_technology, device_extra.open_idfa, device_extra.apple_idfa, device_extra.apple_idfv, device_extra.android_idfa, device_extra.battery_level, device_extra.battery_state, device_extra.low_power_mode),
    (resolution.browser, resolution.viewport, resolution.page),
    (event.action, event.category, event.label, if(isValidJSON(event.property), event.property, '{}'), event.value, if(isValidJSON(event.unstructured), event.unstructured, '{}')),
    if(isValidJSON(extra), extra, '{}'),
    (tracker.version, tracker.namespace)
FROM snowplow.old;
```

---

## [v1.1.0] - 2024-08-30

### Changed
- Updated column definitions for improved type safety
- Enhanced datetime precision with timezone support
- Improved device and browser data structures

### Migration Instructions

Apply the following schema modifications:

```sql
ALTER TABLE snowplow.local MODIFY COLUMN `time_extra` Tuple(user DateTime64(3, 'UTC'), sent DateTime64(3, 'UTC'));
ALTER TABLE snowplow.local MODIFY COLUMN `browser` Tuple(family LowCardinality(String), version String, cookie Bool, charset LowCardinality(String), color_depth UInt8, unstructured String);
ALTER TABLE snowplow.local MODIFY COLUMN `device_is` Tuple(mobile Bool, tablet Bool, touch Bool, pc Bool, bot Bool);
ALTER TABLE snowplow.local MODIFY COLUMN `device_extra` Tuple(carrier LowCardinality(String), network_type LowCardinality(String), network_technology LowCardinality(String), open_idfa String, apple_idfa String, apple_idfv String, android_idfa String, battery_level UInt8, battery_state LowCardinality(String), low_power_mode Bool);
```

---

## Notes

- Always backup your data before running migration scripts
- Test migrations in a development environment first
- Breaking changes are marked with **BREAKING** for easy identification
- Version numbers follow semantic versioning (MAJOR.MINOR.PATCH)
