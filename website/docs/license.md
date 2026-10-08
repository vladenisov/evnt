# License and attribution

This project's own source code is licensed under BSD 3-Clause (see [LICENSE](https://github.com/vladenisov/evnt/blob/main/LICENSE)).

It interoperates with, and optionally redistributes unmodified copies of, third-party components from Snowplow Analytics Ltd. and other authors:

- **Snowplow JavaScript tracker** (`sp.js`, plugins) — BSD 3-Clause, © 2022 Snowplow Analytics Ltd, © 2010 Anthon Pang. Fetched by `evnt scripts download` at image build time; not committed to this repo.
- **Iglu Central schemas** — Apache License 2.0, © Snowplow Analytics Ltd. Included as a git submodule at `backend/vendor/iglu-central`, unmodified.

Full third-party copyright and license notices are in [THIRD_PARTY_NOTICES.md](https://github.com/vladenisov/evnt/blob/main/THIRD_PARTY_NOTICES.md), which downstream packagers **must** redistribute alongside any Docker image or artifact that bundles the tracker scripts or Iglu schemas.

"Snowplow" is a trademark of Snowplow Analytics Ltd. This project is not affiliated with, sponsored by, or endorsed by Snowplow Analytics Ltd.
