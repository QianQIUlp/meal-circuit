from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.version import android_version_code, check, project_version


class VersionContractTest(unittest.TestCase):
    def test_pyproject_version_is_the_android_input(self) -> None:
        version = project_version()
        self.assertEqual(version, "0.3.1")
        self.assertEqual(android_version_code(version), 30001)
        self.assertEqual(android_version_code("0.3.1"), 30001)

    def test_invalid_semver_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            android_version_code("0.3")

    def test_check_can_emit_github_job_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            check(f"v{project_version()}", output)
            self.assertEqual(output.read_text(encoding="utf-8"), "version=0.3.1\n")


if __name__ == "__main__":
    unittest.main()
