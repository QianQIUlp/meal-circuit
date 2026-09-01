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

    def test_packaged_windows_webview_must_be_exercised_with_a_timeout(self):
        for snippet in (
            "- name: Exercise the packaged Windows WebView",
            '-ArgumentList "--ui-smoke-test" -PassThru',
            "$process.WaitForExit(70000)",
            "Stop-Process -Id $process.Id -Force",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.workflow)
        invalid = self.workflow.replace(
            "- name: Exercise the packaged Windows WebView",
            "- name: Skip the packaged Windows WebView",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows packaged WebView"):
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

    def test_partial_android_credentials_are_rejected(self):
        incomplete = self.workflow.replace(
            " && secrets.ANDROID_KEY_PASSWORD != '' }}",
            " }}",
        )
        with self.assertRaisesRegex(SystemExit, "ANDROID_SIGNING_AVAILABLE"):
            check_release_workflow(incomplete)

    def test_unsigned_android_artifacts_cannot_reach_a_tagged_release(self):
        invalid = self.workflow.replace(
            "- name: Require Android signing secrets for tagged releases",
            "- name: Allow unsigned Android tagged release",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Android signing is mandatory"):
            check_release_workflow(invalid)

    def test_unsigned_android_validation_builds_are_normalized_but_never_tagged(self):
        for snippet in (
            "- name: Normalize the Android APK path for unsigned validation builds",
            "if: env.ANDROID_SIGNING_AVAILABLE != 'true'",
            'if [[ "$GITHUB_REF" == refs/tags/v* ]]; then',
            "app-release-unsigned.apk",
            'install -m 644 "$unsigned_apk" "$canonical_apk"',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.workflow)
        invalid = self.workflow.replace(
            'if [[ "$GITHUB_REF" == refs/tags/v* ]]; then',
            'if [[ "$GITHUB_REF" == refs/heads/main ]]; then',
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "unsigned Android validation builds"):
            check_release_workflow(invalid)

    def test_strict_aab_verification_uses_the_restored_release_key(self):
        invalid = self.workflow.replace(
            'android/app/build/outputs/bundle/release/app-release.aab "$ANDROID_KEY_ALIAS"',
            'android/app/build/outputs/bundle/release/app-release.aab "$UNTRUSTED_ALIAS"',
        )
        with self.assertRaisesRegex(SystemExit, "Android signer continuity"):
            check_release_workflow(invalid)

    def test_android_apk_signer_must_match_the_v030_certificate(self):
        invalid = self.workflow.replace(
            'if [[ "${apk_fingerprints[0]}" != "$expected_fingerprint" ]];',
            'if [[ -z "${apk_fingerprints[0]}" ]];',
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Android signer continuity"):
            check_release_workflow(invalid)

    def test_android_keystore_alias_must_match_the_v030_certificate(self):
        invalid = self.workflow.replace(
            'if [[ "$keystore_fingerprint" != "$expected_fingerprint" ]];',
            'if [[ -z "$keystore_fingerprint" ]];',
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Android signer continuity"):
            check_release_workflow(invalid)

    def test_android_signer_fingerprint_cannot_change_silently(self):
        invalid = self.workflow.replace(
            "1A:87:F4:05:CA:18:9B:1D:B2:63:44:A9:71:55:18:C9:12:4C:66:D5:D8:00:04:CA:75:A5:9F:EA:36:25:3C:37",
            "00:87:F4:05:CA:18:9B:1D:B2:63:44:A9:71:55:18:C9:12:4C:66:D5:D8:00:04:CA:75:A5:9F:EA:36:25:3C:37",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Android signer continuity"):
            check_release_workflow(invalid)

    def test_android_release_assets_must_be_flattened_for_the_release_job(self):
        invalid = self.workflow.replace(
            "path: ${{ runner.temp }}/android-release/*",
            "path: android/app/build/outputs/apk/release/*.apk",
        )
        with self.assertRaisesRegex(SystemExit, "Android release assets are flattened"):
            check_release_workflow(invalid)

    def test_final_android_artifacts_must_be_legally_inspected(self):
        for snippet in (
            "- name: Verify legal assets in final Android artifacts",
            "python tools/android_artifact_legal_check.py --license LICENSE",
            "android/app/build/outputs/apk/release/app-release.apk",
            "android/app/build/outputs/bundle/release/app-release.aab",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.workflow)
        invalid = self.workflow.replace(
            "- name: Verify legal assets in final Android artifacts",
            "- name: Skip legal assets in final Android artifacts",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "final Android artifacts"):
            check_release_workflow(invalid)

    def test_desktop_tagged_release_requires_signing(self):
        invalid = self.workflow.replace(
            "- name: Require Authenticode secrets for tagged releases",
            "- name: Warn about missing Authenticode secrets",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "Windows signing is mandatory"):
            check_release_workflow(invalid)

    def test_release_job_must_depend_on_quality_gate_and_maintained_platforms(self):
        incomplete = self.workflow.replace(
            "needs: [contract, quality-gate, windows, android]",
            "needs: [contract, windows, android]",
        )
        with self.assertRaisesRegex(SystemExit, "waits for the complete test workflow"):
            check_release_workflow(incomplete)

    def test_quality_gate_selects_the_tag_ref_not_an_older_run_on_the_same_sha(self):
        self.assertIn("--json status,conclusion,url,createdAt,headBranch", self.workflow)
        self.assertIn(
            "map(select(.headBranch == env.GITHUB_REF_NAME))",
            self.workflow,
        )
        invalid = self.workflow.replace(
            "map(select(.headBranch == env.GITHUB_REF_NAME)) | ",
            "",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "complete test workflow"):
            check_release_workflow(invalid)

    def test_quality_gate_allows_for_slow_tag_runner_queues(self):
        invalid = self.workflow.replace(
            "deadline=$((SECONDS + 10800))",
            "deadline=$((SECONDS + 3600))",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "complete test workflow"):
            check_release_workflow(invalid)

    def test_final_desktop_artifacts_are_smoke_tested_and_legally_inspected(self):
        for snippet in (
            'tools\\desktop_licenses.py verify --project-root . --artifact-root "$portableRoot\\MealCircuit"',
            'tools\\desktop_licenses.py verify --project-root . --artifact-root $installDir',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.workflow)
        incomplete = self.workflow.replace(
            "- name: Collect locked desktop license texts",
            "- name: Skip locked desktop license texts",
            1,
        )
        self.assertNotEqual(self.workflow, incomplete)
        with self.assertRaisesRegex(SystemExit, "maintained Windows environment"):
            check_release_workflow(incomplete)

    def test_tagged_release_requires_private_vulnerability_reporting(self):
        invalid = self.workflow.replace(
            "- name: Require private vulnerability reporting for tagged releases",
            "- name: Skip private vulnerability reporting check",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "waits for the complete test workflow"):
            check_release_workflow(invalid)

    def test_release_does_not_merge_intermediate_slice_artifacts(self):
        invalid = self.workflow.replace(
            "          name: desktop-windows\n          path: release-assets",
            "          name: desktop-windows\n          path: release-assets\n          merge-multiple: true",
            1,
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "must not merge intermediate artifacts"):
            check_release_workflow(invalid)

    def test_release_uses_lock_derived_sbom(self):
        invalid = self.workflow.replace(
            "Generate lock-derived CycloneDX SBOM",
            "Scan assembled artifacts for a partial SBOM",
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "lock-derived SBOM"):
            check_release_workflow(invalid)

    def test_final_platform_uploads_fail_when_files_are_missing(self):
        for artifact in ("desktop-windows", "android-release"):
            with self.subTest(artifact=artifact):
                invalid = self.workflow.replace(
                    f"name: {artifact}\n          if-no-files-found: error",
                    f"name: {artifact}",
                    1,
                )
                self.assertNotEqual(self.workflow, invalid)
                with self.assertRaisesRegex(SystemExit, "upload[s]? fail[s]? closed"):
                    check_release_workflow(invalid)

    def test_release_scope_excludes_unmaintained_desktop_platforms(self):
        invalid = self.workflow.replace(
            "  android:\n",
            "  macos-build:\n    runs-on: macos-15\n    steps: []\n\n  android:\n",
            1,
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "only Windows x64 and Android"):
            check_release_workflow(invalid)

    def test_final_manifest_contains_exactly_four_maintained_assets(self):
        invalid = self.workflow.replace(
            '            "app-release.aab"',
            '            "app-release.aab"\n            "unexpected-platform-artifact.bin"',
        )
        self.assertNotEqual(self.workflow, invalid)
        with self.assertRaisesRegex(SystemExit, "exactly Windows x64 and Android assets"):
            check_release_workflow(invalid)


if __name__ == "__main__":
    unittest.main()
