from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STABLE_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class VersioningPolicyTests(unittest.TestCase):
    def test_app_version_is_stable_semver(self):
        from pet.version import APP_VERSION
        self.assertRegex(APP_VERSION, STABLE_SEMVER)

    def test_policy_and_changelog_exist(self):
        self.assertTrue((ROOT / "VERSIONING.md").is_file())
        self.assertTrue((ROOT / "CHANGELOG.md").is_file())
        policy = (ROOT / "VERSIONING.md").read_text(encoding="utf-8")
        self.assertIn("Semantic Versioning 2.0.0", policy)
        self.assertIn("public compatibility contract", policy)
        self.assertIn("published release tag", policy.lower())

    def test_release_workflow_enforces_tag_version_equality_and_draft_first(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")
        self.assertIn("stable SemVer", workflow)
        self.assertIn("^v(0|[1-9]\\d*)", workflow)
        self.assertIn('if ($tag -ne "v$version")', workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("gh release edit", workflow)
        self.assertIn("--draft=false", workflow)

    def test_readme_links_versioning_documents(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("VERSIONING.md", readme)
        self.assertIn("CHANGELOG.md", readme)


if __name__ == "__main__":
    unittest.main()
