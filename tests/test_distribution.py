from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mealcircuit.configuration import configuration_status, initialize_private_home
from mealcircuit.contracts import load_contract
from mealcircuit.resources import resource_path
from tools.distribution_check import DistributionCheckError, _assert_clean_members


ROOT = Path(__file__).resolve().parents[1]


class DistributionResourceTest(unittest.TestCase):
    def test_pep_639_build_backend_is_reproducibly_pinned(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            build_system = tomllib.load(handle)["build-system"]
        self.assertEqual(["setuptools==83.0.0"], build_system["requires"])

    def test_source_resources_resolve(self) -> None:
        self.assertTrue(resource_path("rules", "core.md").is_file())
        self.assertTrue(resource_path("templates", "profile.md").is_file())
        self.assertEqual(load_contract("state-machines-v1.json")["schema_version"], 1)

    def test_fresh_template_is_a_valid_onboarding_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mealcircuit-fresh-home-") as temporary:
            home = Path(temporary) / "home"
            with patch.dict(
                "os.environ",
                {"MEALCIRCUIT_HOME": str(home)},
                clear=False,
            ):
                initialize_private_home()
                status = configuration_status()
        self.assertTrue(status["settings_valid"], status["settings_error"])
        self.assertEqual(status["doctrine_mode"], "composed")

    def test_resource_names_cannot_escape_the_bundle(self) -> None:
        with self.assertRaises(ValueError):
            resource_path("templates", "../settings.json")
        with self.assertRaises(ValueError):
            resource_path("../templates", "settings.json")

    def test_distribution_archive_rejects_compiled_cache(self) -> None:
        with self.assertRaises(DistributionCheckError):
            _assert_clean_members(
                ["mealcircuit/__pycache__/storage.cpython-312.pyc"],
                Path("candidate.whl"),
            )


if __name__ == "__main__":
    unittest.main()
