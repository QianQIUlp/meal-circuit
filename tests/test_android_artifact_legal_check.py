from __future__ import annotations

import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from tools.android_artifact_legal_check import ArtifactLegalError, verify_android_artifact


ROOT = Path(__file__).resolve().parents[1]
LEGAL_SOURCE = ROOT / "android" / "app" / "src" / "main" / "assets" / "legal"
LICENSE = ROOT / "LICENSE"


class AndroidArtifactLegalCheckTest(unittest.TestCase):
    def _artifact(
        self,
        directory: Path,
        suffix: str,
        *,
        omitted: str | None = None,
        tampered: str | None = None,
        extra_members: tuple[tuple[str, bytes], ...] = (),
    ) -> Path:
        path = directory / f"app-release{suffix}"
        prefix = "assets/legal" if suffix == ".apk" else "base/assets/legal"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
            for source in sorted(LEGAL_SOURCE.iterdir()):
                if not source.is_file() or source.name == omitted:
                    continue
                data = source.read_bytes()
                if source.name == "MEALCIRCUIT_LICENSE.txt":
                    # A checkout may use CRLF while Android assets conventionally
                    # use LF; the verifier permits only this newline normalization.
                    data = LICENSE.read_bytes().replace(b"\r\n", b"\n")
                if source.name == tampered:
                    data = data.replace(b"MealCircuit", b"TamperedCircuit", 1)
                archive.writestr(f"{prefix}/{source.name}", data)
            for name, data in extra_members:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, data)
        return path

    def test_accepts_complete_apk_and_aab_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for suffix, prefix in ((".apk", "assets/legal"), (".aab", "base/assets/legal")):
                with self.subTest(suffix=suffix):
                    result = verify_android_artifact(self._artifact(directory, suffix), LICENSE)
                    self.assertTrue(result["ok"])
                    self.assertEqual(prefix, result["legal_prefix"])
                    self.assertEqual(6, len(result["legal_files"]))

    def test_rejects_missing_legal_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = self._artifact(
                Path(temporary), ".apk", omitted="THIRD_PARTY_NOTICES.md"
            )
            with self.assertRaisesRegex(ArtifactLegalError, "missing legal assets.*THIRD_PARTY"):
                verify_android_artifact(artifact, LICENSE)

    def test_rejects_tampered_project_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = self._artifact(
                Path(temporary), ".aab", tampered="MEALCIRCUIT_LICENSE.txt"
            )
            with self.assertRaisesRegex(ArtifactLegalError, "does not exactly match"):
                verify_android_artifact(artifact, LICENSE)

    def test_rejects_truncated_or_marker_stripped_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = self._artifact(
                Path(temporary), ".apk", tampered="THIRD_PARTY_NOTICES.md"
            )
            with self.assertRaisesRegex(ArtifactLegalError, "missing required markers.*MealCircuit"):
                verify_android_artifact(artifact, LICENSE)

    def test_rejects_duplicate_and_traversal_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            duplicate = self._artifact(
                directory,
                ".apk",
                extra_members=(("assets/legal/PRIVACY.md", b"ambiguous"),),
            )
            with self.assertRaisesRegex(ArtifactLegalError, "duplicate ZIP member"):
                verify_android_artifact(duplicate, LICENSE)

            traversal = self._artifact(
                directory,
                ".aab",
                extra_members=(("base/assets/legal/../SECURITY.md", b"unsafe"),),
            )
            with self.assertRaisesRegex(ArtifactLegalError, "unsafe ZIP member"):
                verify_android_artifact(traversal, LICENSE)

    def test_duplicate_entries_cannot_hide_a_tampered_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = self._artifact(Path(temporary), ".apk")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(artifact, "a") as archive:
                    archive.writestr(
                        "assets/legal/MEALCIRCUIT_LICENSE.txt", b"replacement after valid entry"
                    )
            with self.assertRaisesRegex(ArtifactLegalError, "duplicate ZIP member"):
                verify_android_artifact(artifact, LICENSE)


if __name__ == "__main__":
    unittest.main()
