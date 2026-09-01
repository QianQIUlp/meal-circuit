from __future__ import annotations

import json
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang: str | None = None
        self.h1_count = 0
        self.scripts = 0
        self.references: list[str] = []
        self.blank_links_without_noreferrer: list[str] = []
        self.alternates: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.html_lang = values.get("lang")
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "script":
            self.scripts += 1

        for key in ("href", "src"):
            reference = values.get(key)
            if reference:
                self.references.append(reference)

        if tag == "a" and values.get("target") == "_blank":
            rel = set((values.get("rel") or "").split())
            if "noreferrer" not in rel:
                self.blank_links_without_noreferrer.append(values.get("href") or "")

        if tag == "link" and "alternate" in (values.get("rel") or "").split():
            language = values.get("hreflang")
            href = values.get("href")
            if language and href:
                self.alternates[language] = href


def _local_reference_exists(reference: str) -> bool:
    if not reference.startswith("/") or reference.startswith("//"):
        return True
    path = reference.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return True
    target = SITE / path.lstrip("/")
    if path.endswith("/"):
        target /= "index.html"
    return target.is_file()


class ProductSiteTest(unittest.TestCase):
    def test_cloudflare_pages_configuration_points_to_static_site(self):
        config = json.loads((ROOT / "wrangler.jsonc").read_text(encoding="utf-8"))
        self.assertEqual(config["name"], "meal-circuit")
        self.assertEqual(config["pages_build_output_dir"], "./site")
        self.assertRegex(config["compatibility_date"], r"^20\d{2}-\d{2}-\d{2}$")
        self.assertFalse(config["send_metrics"])

    def test_bilingual_pages_are_static_and_self_contained(self):
        expected = {
            "index.html": "en",
            "zh/index.html": "zh-CN",
        }
        for relative_path, language in expected.items():
            with self.subTest(page=relative_path):
                parser = _PageParser()
                parser.feed((SITE / relative_path).read_text(encoding="utf-8"))
                self.assertEqual(parser.html_lang, language)
                self.assertEqual(parser.h1_count, 1)
                self.assertEqual(parser.scripts, 0)
                self.assertEqual(parser.blank_links_without_noreferrer, [])
                self.assertEqual(parser.alternates.get("en"), "/")
                self.assertEqual(parser.alternates.get("zh-CN"), "/zh/")
                self.assertTrue(
                    all(_local_reference_exists(reference) for reference in parser.references),
                    parser.references,
                )

    def test_cloudflare_static_controls_are_present(self):
        headers = (SITE / "_headers").read_text(encoding="utf-8")
        for policy in (
            "Content-Security-Policy:",
            "Permissions-Policy:",
            "Referrer-Policy:",
            "X-Content-Type-Options:",
            "X-Frame-Options:",
        ):
            self.assertIn(policy, headers)
        self.assertIn("404 · OPEN CIRCUIT", (SITE / "404.html").read_text(encoding="utf-8"))
        self.assertTrue((SITE / "robots.txt").is_file())
        self.assertTrue((SITE / "site.webmanifest").is_file())
        self.assertTrue((SITE / "favicon.svg").is_file())

    def test_release_identity_and_planning_boundary_are_current(self):
        english = (SITE / "index.html").read_text(encoding="utf-8")
        chinese = (SITE / "zh" / "index.html").read_text(encoding="utf-8")
        self.assertIn("releases/tag/v0.3.1", english)
        self.assertIn("releases/tag/v0.3.1", chinese)
        self.assertIn("RELEASE 0.3.1", english)
        self.assertIn("0.3.1 正式版", chinese)
        self.assertNotIn("releases/tag/v0.3.0", english)
        self.assertNotIn("releases/tag/v0.3.0", chinese)
        self.assertIn("No bundled planner model", english)
        self.assertIn("不捆绑规划模型", chinese)
        self.assertIn("Android consumes reviewed plans", english)
        self.assertIn("Android 只消费", chinese)
        self.assertIn("Windows x64 · Android", english)
        self.assertIn("Windows x64 · Android", chinese)
        for retired_platform in ("macOS", "Linux", "AppImage", ".dmg"):
            self.assertNotIn(retired_platform, english)
            self.assertNotIn(retired_platform, chinese)


if __name__ == "__main__":
    unittest.main()
