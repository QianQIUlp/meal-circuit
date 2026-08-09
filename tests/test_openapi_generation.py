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
CANONICAL_SHA256 = "5798b82c626918dd92058e655421577dd129a390f624a0ae4990b440b91d6939"
CANONICAL_LF_COUNT = 1158
CANONICAL_CRLF_COUNT = 0


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

    def _assert_canonical_bytes(self, destination: Path) -> bytes:
        raw = destination.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CANONICAL_SHA256)
        self.assertEqual(raw.count(b"\n"), CANONICAL_LF_COUNT)
        self.assertEqual(raw.count(b"\r\n"), CANONICAL_CRLF_COUNT)
        return raw

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

            clean_bytes = self._assert_canonical_bytes(clean)
            polluted_bytes = self._assert_canonical_bytes(polluted)
            self.assertEqual(clean_bytes, polluted_bytes)


if __name__ == "__main__":
    unittest.main()
