from __future__ import annotations

import unittest
from pathlib import Path

from tools.dependency_check import check_release_workflow


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_current_release_workflow_satisfies_signing_policy(self):
        check_release_workflow(self.workflow)

    def test_partial_windows_credentials_are_rejected(self):
        incomplete = self.workflow.replace(
            "WINDOWS_SIGNING_AVAILABLE: ${{ secrets.WINDOWS_CERTIFICATE_BASE64 != '' && secrets.WINDOWS_CERTIFICATE_PASSWORD != '' }}",
            "WINDOWS_SIGNING_AVAILABLE: ${{ secrets.WINDOWS_CERTIFICATE_BASE64 != '' }}",
        )
        with self.assertRaisesRegex(SystemExit, "WINDOWS_SIGNING_AVAILABLE"):
            check_release_workflow(incomplete)

    def test_partial_apple_credentials_are_rejected(self):
        incomplete = self.workflow.replace(
            " && secrets.APPLE_TEAM_ID != '' }}",
            " }}",
        )
        with self.assertRaisesRegex(SystemExit, "APPLE_SIGNING_AVAILABLE"):
            check_release_workflow(incomplete)

    def test_partial_android_credentials_are_rejected(self):
        incomplete = self.workflow.replace(
            " && secrets.ANDROID_KEY_PASSWORD != '' }}",
            " }}",
        )
        with self.assertRaisesRegex(SystemExit, "ANDROID_SIGNING_AVAILABLE"):
            check_release_workflow(incomplete)

    def test_unsigned_android_artifacts_cannot_reach_a_tagged_release(self):
        invalid = self.workflow.replace(
            "if: ${{ !startsWith(github.ref, 'refs/tags/v') || env.ANDROID_SIGNING_AVAILABLE == 'true' }}",
            "if: always()",
        )
        with self.assertRaisesRegex(SystemExit, "Android tagged-release omission policy"):
            check_release_workflow(invalid)

    def test_strict_aab_verification_uses_the_restored_release_key(self):
        invalid = self.workflow.replace(
            'android/app/build/outputs/bundle/release/app-release.aab "$ANDROID_KEY_ALIAS"',
            'android/app/build/outputs/bundle/release/app-release.aab "$UNTRUSTED_ALIAS"',
        )
        with self.assertRaisesRegex(SystemExit, "Android strict AAB signer verification"):
            check_release_workflow(invalid)

    def test_android_release_assets_must_be_flattened_for_the_release_job(self):
        invalid = self.workflow.replace(
            "path: ${{ runner.temp }}/android-release/*",
            "path: android/app/build/outputs/apk/release/*.apk",
        )
        with self.assertRaisesRegex(SystemExit, "Android release assets are flattened"):
            check_release_workflow(invalid)

    def test_desktop_hard_gate_is_rejected(self):
        invalid = self.workflow + "\n# Require Authenticode secrets for tagged releases\n"
        with self.assertRaisesRegex(SystemExit, "must not hard-fail"):
            check_release_workflow(invalid)

    def test_release_job_must_depend_on_every_platform(self):
        incomplete = self.workflow.replace(
            "needs: [windows, macos-universal, linux, android]",
            "needs: [windows, macos-universal, linux]",
        )
        with self.assertRaisesRegex(SystemExit, "release build dependencies"):
            check_release_workflow(incomplete)


if __name__ == "__main__":
    unittest.main()
