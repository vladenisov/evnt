"""Validate a release before publishing images or creating a GitHub tag."""

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    tag: str
    sha: str
    is_release: bool


def resolve(
    version: str,
    event: str,
    ref: str,
    requested: str,
    sha: str,
    existing_tag_sha: str | None = None,
) -> ReleaseMetadata:
    if event not in {"push", "workflow_dispatch"}:
        raise ValueError(f"Unsupported release event: {event}")
    is_release = event == "workflow_dispatch" or ref.startswith("refs/tags/")
    if event == "workflow_dispatch" and ref != "refs/heads/main":
        raise ValueError("Run manual releases from main")
    if not is_release and ref != "refs/heads/main":
        raise ValueError("Only main publishes the latest image")
    tag = f"v{version}"
    if is_release:
        if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
            raise ValueError("Releases require a stable X.Y.Z package version")
        if event == "workflow_dispatch" and requested != version:
            raise ValueError(f"Requested {requested!r}, but backend version is {version!r}")
        if event == "push" and ref != f"refs/tags/{tag}":
            raise ValueError(f"Release tag must match the backend version: {tag}")
        if existing_tag_sha is not None and existing_tag_sha != sha:
            raise ValueError(f"{tag} already points to another commit; bump the package version")
    return ReleaseMetadata(version, tag, sha, is_release)


def lookup_tag_sha(tag: str) -> str | None:
    output = subprocess.check_output(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        text=True,
    )
    refs = {ref: sha for sha, ref in (line.split() for line in output.splitlines())}
    return refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))


def main() -> None:
    project = tomllib.loads(Path("backend/pyproject.toml").read_text())
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    args = (
        project["project"]["version"],
        os.environ["GITHUB_EVENT_NAME"],
        os.environ["GITHUB_REF"],
        os.environ.get("REQUESTED_VERSION", ""),
        sha,
    )
    metadata = resolve(*args)
    if metadata.is_release:
        metadata = resolve(*args, existing_tag_sha=lookup_tag_sha(metadata.tag))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"version={metadata.version}\ntag={metadata.tag}\nsha={metadata.sha}\n")
        output.write(f"is_release={str(metadata.is_release).lower()}\n")
    print(f"Validated {metadata.tag} at {metadata.sha}; release={metadata.is_release}")


if __name__ == "__main__":
    main()
