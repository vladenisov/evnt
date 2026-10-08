# Publishing releases

Version history is maintained in [GitHub Releases](https://github.com/vladenisov/evnt/releases).
The **Release** workflow publishes a versioned Docker image and release notes
after all CI checks succeed. It supports stable `X.Y.Z` versions.

## Prepare a version

The release version comes from `backend/pyproject.toml`, not from a separate
release counter. For a subsequent release, bump the package version using uv
from `backend/` and update `backend/uv.lock`, then commit both files to `main`.
The first release can publish the existing package version.

Use clear PR titles and descriptions. For breaking changes, label the PR
`breaking-change`, describe the migration in [Upgrading](upgrading.md), and
make the change explicit in the release notes before announcing it.

## Publish from GitHub Actions

1. Open [Actions → Release](https://github.com/vladenisov/evnt/actions/workflows/release.yml).
2. Click **Run workflow** and choose **main**.
3. Enter the expected package version, without the `v` prefix, in **version**.
4. Run the workflow and wait for validation, CI, tag reservation, image
   publication, and GitHub Release notes to succeed.

The entered version must match `backend/pyproject.toml` at the selected commit.
After CI succeeds, the workflow creates its `vX.Y.Z` tag. It then publishes
`vladenisov/evnt:X.Y.Z` and `:X.Y` for `linux/amd64` and `linux/arm64`, updates
`:latest` when run from `main`, and creates the GitHub Release. The same
workflow handles tags pushed with Git; a tag must match the package version.

The built-in `GITHUB_TOKEN` creates tags and releases. The image uses the
existing `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets.
Tags created by the workflow do not start another tag workflow; this same
run completes the entire publication.

## Release notes

GitHub generates notes from merged PRs using `.github/release.yml`:

| Category | PR labels |
| --- | --- |
| Breaking changes | `breaking-change`, `breaking` |
| Features | `enhancement`, `feature`, `feat` |
| Fixes | `bug`, `fix` |
| Documentation | `documentation`, `docs` |
| Dependencies | `dependencies` |
| Other changes | All remaining PRs |

Use `skip-changelog` to exclude a PR from the generated notes. Each release
also links to the upgrade guide at that version and identifies its Docker
image. Existing release descriptions are preserved on workflow retries.

## Failures and retries

- A mismatched input, non-stable version, or tag pointing to another commit
  fails validation before CI or image publication. Bump the package version
  for a new commit; never move an existing release tag.
- If CI fails, no image or GitHub Release is published. A manual run creates
  no tag until CI passes.
- If publication fails after a tag is reserved, open the failed run and
  **Re-run all jobs**. This keeps the original commit and version. Starting
  a new run from a changed `main` with the same version will be rejected.
- If a GitHub Release already exists for the same commit, a retry preserves
  its notes. Docker tags may be republished from that same commit.

The release validator is in `.github/scripts/release_metadata.py`, with
standard-library tests covering version checks, retries, real lightweight
and annotated tags, and the pinned commit output. Run them locally from
`backend/` with `uv run python -m unittest discover -s ../.github/scripts`.
