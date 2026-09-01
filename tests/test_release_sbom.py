from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.generate_sbom import ROOT, build_sbom, validate_sbom


class ReleaseSbomTest(unittest.TestCase):
    def test_lock_derived_sbom_covers_release_stacks_without_checkout_paths(self):
        sbom = build_sbom()
        validate_sbom(sbom)
        serialized = json.dumps(sbom)
        self.assertNotIn(str(ROOT.resolve()), serialized)
        self.assertNotIn(str(ROOT.resolve()).replace("\\", "/"), serialized)
        self.assertTrue(any(item["dependsOn"] for item in sbom["dependencies"]))

        components = {item["name"]: item for item in sbom["components"]}
        for name in (
            "mealcircuit",
            "cryptography",
            "keyring",
            "pywebview",
            "okhttp",
            "zxing-android-embedded",
        ):
            with self.subTest(name=name):
                self.assertIn(name, components)
                self.assertIn("pkg:", components[name]["purl"])
        self.assertEqual("0.3.1", components["mealcircuit"]["version"])
        self.assertEqual("50.0.0", components["cryptography"]["version"])

    def test_sbom_can_be_serialized_as_cyclonedx_json(self):
        sbom = build_sbom()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.cdx.json"
            output.write_text(json.dumps(sbom), encoding="utf-8")
            loaded = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("CycloneDX", loaded["bomFormat"])
        self.assertEqual("1.6", loaded["specVersion"])


if __name__ == "__main__":
    unittest.main()
