#!/usr/bin/env python3
"""Release snack-codename label helpers and catalog consistency."""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class TestVersionLabel(unittest.TestCase):
    def test_format_with_both(self):
        self.assertEqual(
            tabit._format_version_label("v1.7.12", "Stinky Tofu"),
            "v1.7.12 · Stinky Tofu",
        )

    def test_format_version_only(self):
        self.assertEqual(tabit._format_version_label("v1.7.12", ""), "v1.7.12")

    def test_release_display_prefers_titled_name(self):
        self.assertEqual(
            tabit._release_display_name("v1.7.12", "v1.7.12 · Bubble Tea"),
            "v1.7.12 · Bubble Tea",
        )

    def test_release_display_snack_only_name(self):
        self.assertEqual(
            tabit._release_display_name("v1.7.12", "Bubble Tea"),
            "v1.7.12 · Bubble Tea",
        )

    def test_release_display_plain_tag_title(self):
        self.assertEqual(
            tabit._release_display_name("v1.7.12", "v1.7.12"),
            "v1.7.12",
        )

    def test_app_codename_wired(self):
        self.assertTrue(tabit.APP_VERSION.startswith("v"))
        self.assertTrue(tabit.APP_CODENAME)
        self.assertIn("·", tabit._format_version_label())
        self.assertEqual(
            tabit._format_version_label(),
            f"{tabit.APP_VERSION} · {tabit.APP_CODENAME}",
        )


class TestCodenameCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "release-codenames.json")
        with open(path, encoding="utf-8") as f:
            cls.data = json.load(f)
        cls.items = cls.data["codenames"]

    def test_expected_snacks_present(self):
        english = {c["english"] for c in self.items}
        expected = {
            "Oyster Omelette",
            "Stinky Tofu",
            "Bubble Tea",
            "Braised Pork Rice",
            "Pepper Buns",
            "Fried Chicken",
            "Scallion Pancake",
            "Beef Noodle",
            "Aiyu Jelly",
            "Mango Shaved Ice",
        }
        self.assertEqual(english, expected)

    def test_no_duplicate_english(self):
        names = [c["english"] for c in self.items]
        self.assertEqual(len(names), len(set(names)))

    def test_used_by_unique(self):
        used = [c["used_by"] for c in self.items if c.get("used_by")]
        self.assertEqual(len(used), len(set(used)))

    def test_app_version_matches_catalog(self):
        hit = next(
            (c for c in self.items if c.get("used_by") == tabit.APP_VERSION),
            None,
        )
        self.assertIsNotNone(
            hit, f"{tabit.APP_VERSION} missing used_by in release-codenames.json")
        self.assertEqual(hit["english"], tabit.APP_CODENAME)

    def test_script_title_matches_app(self):
        # Keep scripts/release-codename.py title in sync with tabit.py constants.
        with open(os.path.join(ROOT, "tabit.py"), encoding="utf-8") as _f:
            text = _f.read()
        ver = re.search(r'^APP_VERSION\s*=\s*"([^"]*)"', text, re.M).group(1)
        code = re.search(r'^APP_CODENAME\s*=\s*"([^"]*)"', text, re.M).group(1)
        self.assertEqual(ver, tabit.APP_VERSION)
        self.assertEqual(code, tabit.APP_CODENAME)


if __name__ == "__main__":
    unittest.main()
