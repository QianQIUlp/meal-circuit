from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ResponsiveUiContractTest(unittest.TestCase):
    def test_mobile_interface_settings_cannot_force_a_wide_second_column(self) -> None:
        css = (ROOT / "mealcircuit" / "static" / "app.css").read_text(encoding="utf-8")
        mobile = css.split("@media (max-width: 767px)", 1)[1].split(
            "@media (max-width: 420px)", 1
        )[0]
        self.assertIn(
            ".interface-settings .settings-row { grid-template-columns: minmax(0, 1fr); }",
            mobile,
        )
        select_rule = mobile.split(".interface-settings .settings-row select {", 1)[1].split(
            "}", 1
        )[0]
        for declaration in (
            "width: 100%;",
            "min-width: 0;",
            "max-width: 100%;",
            "justify-self: stretch;",
        ):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, select_rule)


if __name__ == "__main__":
    unittest.main()
