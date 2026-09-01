from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSyntaxRegressionTest(unittest.TestCase):
    def test_release_workflow_rejects_broken_step_indentation(self) -> None:
        actionlint = os.environ.get("ACTIONLINT_BIN") or shutil.which("actionlint")
        if not actionlint:
            self.skipTest("actionlint is installed by the CI contract job")

        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        broken, replacements = re.subn(
            r"(?m)^        run: ./gradlew",
            "         run: ./gradlew",
            workflow,
            count=1,
        )
        self.assertEqual(replacements, 1)
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "release-broken.yml"
            candidate.write_text(broken, encoding="utf-8")
            result = subprocess.run(
                [actionlint, str(candidate)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
