from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "android" / "app"
LEGAL = APP / "src" / "main" / "assets" / "legal"
UI = APP / "src" / "main" / "java" / "org" / "mealcircuit" / "app" / "ui"


class AndroidLegalReleaseGateTest(unittest.TestCase):
    def test_project_and_third_party_licenses_are_release_assets(self) -> None:
        expected = {
            "MEALCIRCUIT_LICENSE.txt",
            "THIRD_PARTY_NOTICES.md",
            "APACHE-2.0.txt",
            "PRIVACY.md",
            "SECURITY.md",
            "DISCLAIMER.md",
        }
        self.assertEqual(expected, {path.name for path in LEGAL.iterdir() if path.is_file()})
        self.assertEqual(
            (ROOT / "LICENSE").read_text(encoding="utf-8").strip(),
            (LEGAL / "MEALCIRCUIT_LICENSE.txt").read_text(encoding="utf-8").strip(),
        )
        apache = (LEGAL / "APACHE-2.0.txt").read_text(encoding="utf-8")
        self.assertGreater(len(apache), 10_000)
        self.assertIn("Apache License", apache)
        self.assertIn("END OF TERMS AND CONDITIONS", apache)

        notice = (LEGAL / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for component in ("Kotlin", "AndroidX", "OkHttp", "Okio", "ZXing", "JSpecify"):
            with self.subTest(component=component):
                self.assertIn(component, notice)
        self.assertIn("android/app/gradle.lockfile", notice)

        privacy = (LEGAL / "PRIVACY.md").read_text(encoding="utf-8")
        security = (LEGAL / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("Android application does not call an external AI provider", privacy)
        self.assertIn("private security advisory", security)
        self.assertNotIn("open a minimal public issue", security)

    def test_legal_documents_have_an_offline_in_app_reader(self) -> None:
        more_screen = (UI / "MoreScreen.kt").read_text(encoding="utf-8")
        legal_screen = (UI / "LegalScreen.kt").read_text(encoding="utf-8")
        settings_screen = (UI / "SettingsScreen.kt").read_text(encoding="utf-8")
        self.assertIn('LEGAL("法律与隐私")', more_screen)
        self.assertIn("MoreTab.LEGAL -> LegalScreen()", more_screen)
        self.assertIn("LegalSettingsEntry()", settings_screen)
        self.assertIn('Text("打开法律与隐私文本")', legal_screen)
        for name in (
            "MEALCIRCUIT_LICENSE.txt",
            "THIRD_PARTY_NOTICES.md",
            "APACHE-2.0.txt",
            "PRIVACY.md",
            "SECURITY.md",
            "DISCLAIMER.md",
        ):
            with self.subTest(name=name):
                self.assertIn(f'"legal/{name}"', legal_screen)
        self.assertIn("context.assets.open(selected.assetPath)", legal_screen)

    def test_instrumentation_gate_reads_assets_from_installed_package(self) -> None:
        test_source = (
            APP
            / "src"
            / "androidTest"
            / "java"
            / "org"
            / "mealcircuit"
            / "app"
            / "LegalAssetsInstrumentedTest.kt"
        ).read_text(encoding="utf-8")
        self.assertIn("targetContext.assets", test_source)
        self.assertIn('assets.list("legal")', test_source)
        self.assertIn("END OF TERMS AND CONDITIONS", test_source)

        build = (APP / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertNotIn('sourceSets["main"].assets.setSrcDirs', build)


if __name__ == "__main__":
    unittest.main()
