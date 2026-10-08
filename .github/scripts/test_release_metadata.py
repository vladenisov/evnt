"""Release invariants and real Git tag resolution, with no remote writes."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from release_metadata import lookup_tag_sha, main, resolve


class ReleaseValidationTests(unittest.TestCase):
    def test_manual_release_uses_matching_package_version(self):
        result = resolve("0.6.0", "workflow_dispatch", "refs/heads/main", "0.6.0", "commit")
        self.assertTrue(result.is_release)
        self.assertEqual(result.tag, "v0.6.0")

    def test_manual_release_rejects_other_branch(self):
        with self.assertRaisesRegex(ValueError, "from main"):
            resolve("0.6.0", "workflow_dispatch", "refs/heads/feature", "0.6.0", "commit")

    def test_manual_release_rejects_stale_version_input(self):
        with self.assertRaisesRegex(ValueError, "backend version"):
            resolve("0.6.0", "workflow_dispatch", "refs/heads/main", "0.5.0", "commit")

    def test_main_push_does_not_publish_a_versioned_release(self):
        self.assertFalse(resolve("0.6.0", "push", "refs/heads/main", "", "commit").is_release)

    def test_feature_push_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Only main"):
            resolve("0.6.0", "push", "refs/heads/feature", "", "commit")

    def test_tag_push_requires_matching_package_version(self):
        self.assertTrue(resolve("0.6.0", "push", "refs/tags/v0.6.0", "", "commit").is_release)
        with self.assertRaisesRegex(ValueError, "must match"):
            resolve("0.6.0", "push", "refs/tags/v0.5.0", "", "commit")

    def test_noncanonical_or_prerelease_versions_are_rejected(self):
        for version in ["v0.6.0", "0.6", "0.6.0rc1", "01.6.0", "0.6.0+build", "$(false)"]:
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "stable"):
                resolve(version, "workflow_dispatch", "refs/heads/main", version, "commit")

    def test_version_cannot_be_reused_for_a_different_commit(self):
        with self.assertRaisesRegex(ValueError, "bump the package version"):
            resolve("0.6.0", "workflow_dispatch", "refs/heads/main", "0.6.0", "new", "old")

    def test_same_commit_can_be_retried(self):
        self.assertTrue(
            resolve(
                "0.6.0", "workflow_dispatch", "refs/heads/main", "0.6.0", "same", "same"
            ).is_release
        )

    def test_unexpected_trigger_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            resolve("0.6.0", "pull_request", "refs/pull/1/merge", "", "commit")


class GitTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Release tests")
        self.git("config", "user.email", "release-tests@example.invalid")
        self.git("commit", "--quiet", "--allow-empty", "-m", "test")
        self.git("remote", "add", "origin", str(self.repo))
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.previous_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.previous_cwd)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def test_absent_tag(self):
        self.assertIsNone(lookup_tag_sha("v0.6.0"))

    def test_lightweight_tag(self):
        self.git("tag", "v0.6.0")
        self.assertEqual(lookup_tag_sha("v0.6.0"), self.sha)

    def test_annotated_tag_resolves_to_commit_not_tag_object(self):
        self.git("tag", "-a", "v0.6.0", "-m", "release")
        self.assertNotEqual(self.git("rev-parse", "v0.6.0").strip(), self.sha)
        self.assertEqual(lookup_tag_sha("v0.6.0"), self.sha)

    def test_metadata_output_pins_the_checked_out_commit(self):
        backend = self.repo / "backend"
        backend.mkdir()
        (backend / "pyproject.toml").write_text('[project]\nversion = "0.6.0"\n')
        output = self.repo / "outputs"
        with patch.dict(
            os.environ,
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REF="refs/heads/main",
            REQUESTED_VERSION="0.6.0",
            GITHUB_OUTPUT=str(output),
        ):
            main()
        self.assertIn(f"sha={self.sha}\n", output.read_text())
        self.assertIn("is_release=true\n", output.read_text())


if __name__ == "__main__":
    unittest.main()
