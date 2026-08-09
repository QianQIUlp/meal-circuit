from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools/generate_openapi.py"
CANONICAL_SHA256 = "770f304570b7afb9ed4151af8dd78084d3c4e769d7b3db2b1f802fcc3af01365"


class OpenAPIGenerationTest(unittest.TestCase):
    def _generate(self, destination: Path, **sync_environment: str) -> None:
        environment = os.environ.copy()
        environment.update(sync_environment)
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--output", str(destination)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sync_environment_cannot_change_canonical_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clean = Path(temporary) / "clean.json"
            polluted = Path(temporary) / "polluted.json"
            self._generate(clean)
            self._generate(
                polluted,
                MEALCIRCUIT_SYNC_MAX_PULL="10",
                MEALCIRCUIT_SYNC_MAX_ENTITY_BYTES="2048",
                MEALCIRCUIT_SYNC_MAX_PULL_RESPONSE_BYTES="8192",
                MEALCIRCUIT_SYNC_MAX_ENTITIES="1",
            )

            self.assertEqual(hashlib.sha256(clean.read_bytes()).hexdigest(), CANONICAL_SHA256)
            self.assertEqual(clean.read_bytes(), polluted.read_bytes())


if __name__ == "__main__":
    unittest.main()
