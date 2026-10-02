# Third-Party Notices

This project (`evnt`) is licensed under BSD-3-Clause (see [LICENSE](LICENSE)).
It does **not** include or modify any Snowplow Analytics Ltd. server, collector, or
enrichment code. The Python backend in `evnt/` is original work.

The repository, its build process, or its runtime can pull in the following
third-party components. They remain under their own licenses and copyrights, and
are reproduced or fetched **unmodified**.

---

## Iglu Central (JSON schemas)

- Source: https://github.com/snowplow/iglu-central
- License: Apache License, Version 2.0
- Copyright © 2014-present Snowplow Analytics Ltd.
- How it is used: included as a git submodule at
  `backend/vendor/iglu-central`. Schemas are read at runtime for event
  validation. They are **not modified**.
- License text: `backend/vendor/iglu-central/LICENSE`

A copy of the Apache 2.0 license text is available at
http://www.apache.org/licenses/LICENSE-2.0

## Snowplow JavaScript Tracker (`sp.js`, `loader.js`)

- Source: https://github.com/snowplow/snowplow-javascript-tracker
- License: BSD 3-Clause
- Copyright © 2022 Snowplow Analytics Ltd, © 2010 Anthon Pang
- How it is used: downloaded **unmodified** from the official GitHub Releases
  by `evnt scripts download` (at image build time) and served from
  `/static/sp/`. The tracker bundle is **not committed** to this
  repository (`backend/static/sp/` is gitignored).

## Snowplow Browser Plugins

- Source: https://github.com/snowplow/snowplow-javascript-tracker (subpackages
  under `libraries/browser-plugin-*`)
- License: BSD 3-Clause
- Copyright © 2022 Snowplow Analytics Ltd, © 2010 Anthon Pang
- How it is used: downloaded **unmodified** from the official GitHub Releases
  by `evnt scripts download` (at image build time) and served from
  `/static/sp/plugins/`. Bundles are **not committed** to this
  repository.

The following plugin bundles may be served from this project's `static/sp/plugins/`
directory after running the download CLI; each retains the BSD-3-Clause notice
in its own bundle header:

```
browser-plugin-ad-tracking
browser-plugin-button-click-tracking
browser-plugin-client-hints
browser-plugin-debugger
browser-plugin-element-tracking
browser-plugin-enhanced-consent
browser-plugin-enhanced-ecommerce
browser-plugin-error-tracking
browser-plugin-event-specifications
browser-plugin-focalmeter
browser-plugin-form-tracking
browser-plugin-ga-cookies
browser-plugin-geolocation
browser-plugin-link-click-tracking
browser-plugin-media
browser-plugin-media-tracking
browser-plugin-optimizely-x
browser-plugin-performance-navigation-timing
browser-plugin-performance-timing
browser-plugin-privacy-sandbox
browser-plugin-screen-tracking
browser-plugin-site-tracking
browser-plugin-snowplow-ecommerce
browser-plugin-timezone
browser-plugin-vimeo-tracking
browser-plugin-web-vitals
browser-plugin-webview
browser-plugin-youtube-tracking
```

---

## Trademark notice

"Snowplow" is a trademark of Snowplow Analytics Ltd. References to Snowplow,
the Snowplow protocol, the Snowplow JavaScript tracker, and Iglu Central in
this project's documentation are **nominative fair use** to describe
interoperability and the origin of third-party components. This project is
**not affiliated with, sponsored by, or endorsed by Snowplow Analytics Ltd.**

The licenses above (BSD 3-Clause, Apache 2.0) cover copyright in the source
code. Per BSD-3-Clause clause 3 and Apache-2.0 §6, those licenses do **not**
grant any rights to use the names, trademarks, or logos of the original
authors, and this project does not claim any such rights.

## Redistribution checklist for downstream packagers

If you build and distribute a Docker image, tarball, or other artifact that
**bundles** the JS tracker, plugin files, or Iglu schemas alongside this
project's code, you must also redistribute:

1. This `THIRD_PARTY_NOTICES.md` file, **and**
2. The Apache 2.0 license text shipped with Iglu Central (`LICENSE` file
   inside the `iglu-central` submodule), **and**
3. The original BSD-3-Clause copyright headers embedded in `sp.js`,
   `loader.js`, and each plugin bundle (these are preserved as comments at
   the top of every JS file — do **not** strip them during minification).

If you only redistribute this repository's source (without the bundled
`static/` artifacts and without recursing the submodule), only this file
needs to travel with it.
