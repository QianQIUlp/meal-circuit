from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RELEASE_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "release.md",
    ROOT / "docs" / "releases" / "v0.3.1.md",
    ROOT / "docs" / "multidevice-acceptance.md",
    ROOT / "THIRD_PARTY_LICENSES.md",
)


class SupportedPlatformDocumentationTest(unittest.TestCase):
    def test_current_public_docs_only_claim_windows_and_android(self) -> None:
        for path in PUBLIC_RELEASE_DOCS:
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("Windows", content)
                self.assertIn("Android", content)
                for retired_platform in ("macOS", "Linux", "AppImage", ".dmg"):
                    self.assertNotIn(retired_platform, content)

    def test_v031_release_notes_name_the_exact_six_assets(self) -> None:
        notes = (ROOT / "docs" / "releases" / "v0.3.1.md").read_text(encoding="utf-8")
        expected_assets = (
            "MealCircuit-0.3.1-windows-x64-setup.exe",
            "MealCircuit-0.3.1-windows-x64-portable.zip",
            "app-release.apk",
            "app-release.aab",
            "SHA256SUMS.txt",
            "MealCircuit-v0.3.1.cdx.json",
        )
        for asset in expected_assets:
            self.assertIn(f"`{asset}`", notes)
        self.assertIn("exact six public release assets", notes)


if __name__ == "__main__":
    unittest.main()
