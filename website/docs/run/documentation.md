# Publishing documentation

The Docusaurus site is published at <https://vladenisov.github.io/evnt/> by
the **Publish docs** workflow in `.github/workflows/docs.yml`. It builds with
Bun and uploads a GitHub Pages artifact; the deployment uses the built-in
`GITHUB_TOKEN`. No additional secrets are needed.

## Enable GitHub Pages once

An administrator or maintainer must configure the publishing source:

1. Open the repository's [Settings → Pages](https://github.com/vladenisov/evnt/settings/pages).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.

The repository already contains the publishing workflow, so skip GitHub's
suggested workflow templates. The workflow's token can deploy an enabled
site, but cannot enable GitHub Pages for the first time.

See [GitHub's publishing-source guide](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
for the repository setting.

## Publish the first version

1. Open [Actions → Publish docs](https://github.com/vladenisov/evnt/actions/workflows/docs.yml).
2. Click **Run workflow**, choose **main**, and confirm **Run workflow**.
3. Wait for both the **build** and **deploy** jobs to turn green.
4. Open <https://vladenisov.github.io/evnt/>. The deployment URL is also shown
   in the workflow's `github-pages` environment.

Use a new manual run after enabling Pages if an earlier run failed: it picks
up the latest workflow and documentation from `main`.

## Subsequent updates

Changes under `website/` or to `.github/workflows/docs.yml` pushed to `main`
automatically publish a new version. Manual runs also allow republishing
`main`. Pull requests build and validate the docs in CI before merging;
publication happens from `main`.

The site URL and `/evnt/` base path are configured in
`website/docusaurus.config.ts`. Check types and links before pushing with
`make check-docs`; see [Contributing](../contributing.md#documentation) for
the local workflow.

## Troubleshooting

- **404 / Not Found in Check GitHub Pages configuration**: verify that
  **Settings → Pages → Source** is **GitHub Actions**, then start a new
  workflow run from `main`. Enabling Pages is a separate repository setting.
- **Deployment requires approval or rejects a branch**: inspect
  **Settings → Environments → github-pages**. Its deployment policy must
  allow `main`; approve the run if reviewers are configured.
- **Build fails on a link or type**: run `make check-docs` locally and fix the
  reported error before pushing again. A failed build never deploys.
- **The site still returns 404 after a successful deployment**: use the URL
  reported by the deploy job and verify the `/evnt/` path. Check the Pages
  deployment status in repository Settings if it persists.
