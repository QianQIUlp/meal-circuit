from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID_SOURCE = ROOT / "android" / "app" / "src" / "main" / "java"


class AndroidAuthorityBoundaryTest(unittest.TestCase):
    def test_android_has_no_local_model_or_daily_review_generation_path(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in ANDROID_SOURCE.rglob("*.kt")
        )
        for forbidden in (
            "AiClient",
            "AiProvider",
            "generateDailyReview",
            "saveAiKey",
            "ai.value.generate",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertNotRegex(source, r"\bAiConfiguration\b")

    def test_legacy_ai_cleanup_is_explicit_and_scoped(self) -> None:
        view_model = (
            ANDROID_SOURCE / "org" / "mealcircuit" / "app" / "MainViewModel.kt"
        ).read_text(encoding="utf-8")
        settings = (
            ANDROID_SOURCE / "org" / "mealcircuit" / "app" / "ui" / "SettingsScreen.kt"
        ).read_text(encoding="utf-8")
        self.assertIn("LEGACY_AI_SECRET_NAMES", view_model)
        self.assertIn("app.vault.deleteAll(LEGACY_AI_SECRET_NAMES)", view_model)
        self.assertIn('.remove("ai_provider")', view_model)
        self.assertIn('.remove("ai_model")', view_model)
        self.assertIn("clearLegacyAiConfiguration", settings)
        self.assertIn("确认清理", settings)

    def test_fact_correction_and_sync_paths_remain_present(self) -> None:
        view_model = (
            ANDROID_SOURCE / "org" / "mealcircuit" / "app" / "MainViewModel.kt"
        ).read_text(encoding="utf-8")
        for method in ("addDailyRecord", "addTaskCorrection", "syncNow"):
            with self.subTest(method=method):
                self.assertIn(method, view_model)


if __name__ == "__main__":
    unittest.main()
