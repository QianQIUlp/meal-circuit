from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from tools.desktop_licenses import (
    LicenseBundleError,
    collect_bundle,
    find_bundle,
    verify_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeDistribution:
    def __init__(self, root: Path, name: str, version: str, *, with_license: bool = True):
        self.metadata = {"Name": name}
        self.version = version
        self._root = root
        relative = PurePosixPath(f"{name}-{version}.dist-info/LICENSE.txt")
        self.files = [relative] if with_license else []
        if with_license:
            path = root / Path(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"License text for {name} {version}\n", encoding="utf-8")

    def locate_file(self, entry: object) -> Path:
        return self._root / Path(*PurePosixPath(str(entry)).parts)


class DesktopLicenseBundleTest(unittest.TestCase):
    def distributions(self, root: Path, *, missing_bottle_license: bool = False):
        values = [
            ("cryptography", "50.0.0", True),
            ("keyring", "25.7.0", True),
            ("pyinstaller", "6.21.0", True),
            ("pywebview", "6.2.1", True),
            ("tzdata", "2026.3", True),
            ("proxy_tools", "0.1.0", False),
            ("pyobjc-core", "12.2.1", False),
            ("pyobjc-framework-cocoa", "12.2.1", True),
            ("pyobjc-framework-security", "12.2.1", False),
            ("pyobjc-framework-uniformtypeidentifiers", "12.2.1", False),
        ]
        if missing_bottle_license:
            values.append(("bottle", "0.13.4", False))
        return [
            FakeDistribution(root, name, version, with_license=with_license)
            for name, version, with_license in values
        ]

    def test_bundle_contains_project_python_and_every_distribution_license(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            python_license = temp / "PYTHON-LICENSE.txt"
            python_license.write_text("Python Software Foundation license\n", encoding="utf-8")
            bundle = collect_bundle(
                ROOT,
                temp / "bundle",
                distributions=self.distributions(temp / "packages"),
                python_license=python_license,
            )

            manifest = verify_bundle(
                bundle,
                project_root=ROOT,
                distributions=self.distributions(temp / "verify-packages"),
            )
            self.assertEqual(
                {"LICENSE", "THIRD_PARTY_LICENSES.md", "PRIVACY.md", "SECURITY.md", "DISCLAIMER.md"},
                {item["path"] for item in manifest["project"]},
            )
            self.assertEqual(
                {
                    "cryptography",
                    "keyring",
                    "proxy-tools",
                    "pyobjc-core",
                    "pyobjc-framework-cocoa",
                    "pyobjc-framework-security",
                    "pyobjc-framework-uniformtypeidentifiers",
                    "pyinstaller",
                    "pywebview",
                    "tzdata",
                },
                {item["name"] for item in manifest["distributions"]},
            )
            proxy = next(item for item in manifest["distributions"] if item["name"] == "proxy-tools")
            self.assertEqual(1, len(proxy["licenses"]))
            self.assertTrue((bundle / proxy["licenses"][0]["path"]).is_file())

            embedded = temp / "artifact" / "_internal" / "legal"
            embedded.parent.mkdir(parents=True)
            import shutil

            shutil.copytree(bundle, embedded)
            self.assertEqual(embedded.resolve(), find_bundle(temp / "artifact"))

    def test_unknown_distribution_without_license_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            python_license = temp / "PYTHON-LICENSE.txt"
            python_license.write_text("Python Software Foundation license\n", encoding="utf-8")
            with self.assertRaisesRegex(
                LicenseBundleError,
                "installed distribution has no bundled license text: bottle==0.13.4",
            ):
                collect_bundle(
                    ROOT,
                    temp / "bundle",
                    distributions=self.distributions(
                        temp / "packages", missing_bottle_license=True
                    ),
                    python_license=python_license,
                )

    def test_tampered_embedded_license_fails_verification(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            python_license = temp / "PYTHON-LICENSE.txt"
            python_license.write_text("Python Software Foundation license\n", encoding="utf-8")
            bundle = collect_bundle(
                ROOT,
                temp / "bundle",
                distributions=self.distributions(temp / "packages"),
                python_license=python_license,
            )
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            path = bundle / manifest["distributions"][0]["licenses"][0]["path"]
            path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(LicenseBundleError, "checksum mismatch"):
                verify_bundle(bundle, check_environment=False)


if __name__ == "__main__":
    unittest.main()
