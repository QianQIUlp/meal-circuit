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

    def test_windows_gui_smoke_test_must_wait_and_check_exit_code(self):
        invalid = self.workflow.replace(
            '$process = Start-Process -FilePath ".\\dist\\MealCircuit\\MealCircuit.exe" '
            '-ArgumentList "--smoke-test" -Wait -PassThru',
            '& .\\dist\\MealCircuit\\MealCircuit.exe --smoke-test',
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows packaged smoke test waits"):
            check_release_workflow(invalid)

        unchecked = self.workflow.replace(
            'if ($process.ExitCode -ne 0) { throw "Packaged smoke test failed with exit code $($process.ExitCode)" }',
            'Write-Output "Packaged process started"',
        )
        self.assertNotEqual(self.workflow, unchecked)
        with self.assertRaisesRegex(SystemExit, "Windows packaged smoke test waits"):
            check_release_workflow(unchecked)

    def test_windows_actions_must_use_immutable_pins(self):
        prefix, windows = self.workflow.split("  windows:\n", 1)
        invalid = prefix + "  windows:\n" + windows.replace(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
            "actions/checkout@v4",
            1,
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows actions use immutable pins"):
            check_release_workflow(invalid)

    def test_windows_uv_security_pin_cannot_regress(self):
        prefix, windows = self.workflow.split("  windows:\n", 1)
        invalid = prefix + "  windows:\n" + windows.replace(
            'version: "0.8.22"',
            'version: "0.11.16"',
            1,
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows uv security pin"):
            check_release_workflow(invalid)

    def test_windows_inno_setup_version_must_be_pinned(self):
        invalid = self.workflow.replace(
            "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732",
            "0" * 64,
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows Inno Setup pin"):
            check_release_workflow(invalid)

    def test_windows_artifact_upload_must_fail_closed(self):
        invalid = self.workflow.replace(
            "          name: desktop-windows\n          if-no-files-found: error",
            "          name: desktop-windows",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows artifact upload fails closed"):
            check_release_workflow(invalid)

    def test_windows_final_artifacts_must_be_smoke_tested(self):
        invalid = self.workflow.replace(
            "- name: Smoke test installed application and uninstaller",
            "- name: Skip installed application smoke test",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows final artifacts are smoke tested"):
            check_release_workflow(invalid)

    def test_windows_bundle_excludes_non_windows_webview_and_keyring_components(self):
        spec = (ROOT / "packaging" / "mealcircuit.spec").read_text(encoding="utf-8")
        for module in (
            "webview.platforms.android",
            "webview.platforms.cocoa",
            "webview.platforms.gtk",
            "webview.platforms.qt",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.libsecret",
            "keyring.backends.kwallet",
        ):
            with self.subTest(module=module):
                self.assertIn(f'"{module}"', spec)
        self.assertIn('endswith("webview/lib/pywebview-android.jar")', spec)
        self.assertIn('keyring_hiddenimports.append("keyring.backends.Windows")', spec)

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
            "needs: [contract, windows, macos-universal, linux, android]",
            "needs: [windows, macos-universal, linux]",
        )
        with self.assertRaisesRegex(SystemExit, "release build dependencies"):
            check_release_workflow(incomplete)


if __name__ == "__main__":
    unittest.main()
