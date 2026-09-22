#!/usr/bin/env python3
"""Release cuisine-codename label helpers and shipped-history consistency."""
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

    def test_release_display_codename_only_name(self):
        self.assertEqual(
            tabit._release_display_name("v1.7.12", "Bubble Tea"),
            "v1.7.12 · Bubble Tea",
        )

    def test_release_display_plain_tag_title_unknown(self):
        # Unknown tag with plain GH title stays plain (no history / APP match).
        self.assertEqual(
            tabit._release_display_name("v9.9.9", "v9.9.9"),
            "v9.9.9",
        )

    def test_release_display_plain_tag_falls_back_to_app_codename(self):
        # Older GH titles that are still just the version must match About.
        self.assertEqual(
            tabit._release_display_name(tabit.APP_VERSION, tabit.APP_VERSION),
            f"{tabit.APP_VERSION} · {tabit.APP_CODENAME}",
        )
        self.assertEqual(
            tabit._release_display_name(tabit.APP_VERSION, ""),
            f"{tabit.APP_VERSION} · {tabit.APP_CODENAME}",
        )

    def test_codename_for_tag_from_history(self):
        self.assertEqual(
            tabit._codename_for_tag("v1.7.11"),
            "Oyster Omelette",
        )
        self.assertEqual(tabit._codename_for_tag("v0.0.0"), "")

    def test_app_codename_wired(self):
        self.assertTrue(tabit.APP_VERSION.startswith("v"))
        self.assertTrue(tabit.APP_CODENAME)
        self.assertIn("·", tabit._format_version_label())
        self.assertEqual(
            tabit._format_version_label(),
            f"{tabit.APP_VERSION} · {tabit.APP_CODENAME}",
        )


class TestCodenameHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "release-codenames.json")
        with open(path, encoding="utf-8") as f:
            cls.data = json.load(f)
        cls.items = cls.data["shipped"]

    def test_no_future_candidate_pool(self):
        # Product rule: do not publish upcoming names. Only shipped history.
        self.assertNotIn("codenames", self.data)
        for c in self.items:
            self.assertIn("version", c)
            self.assertIn("english", c)
            self.assertTrue(c["version"])
            self.assertTrue(c["english"])
            # No Chinese / unused / used_by fields in the public history.
            self.assertNotIn("chinese", c)
            self.assertNotIn("used_by", c)

    def test_no_duplicate_english(self):
        """A name belongs to one release, hotfixes of it aside.

        v1.8.3.1 is v1.8.3 with the bugs taken out, so it ships under the
        same name; a different release may not borrow it.
        """
        seen = {}
        for c in self.items:
            name = c["english"].strip().lower()
            base = ".".join(c["version"].lstrip("v").split(".")[:3])
            if name in seen:
                self.assertEqual(
                    seen[name], base,
                    "%r is used by %s and by %s" % (name, seen[name], base))
            else:
                seen[name] = base

    def test_no_duplicate_versions(self):
        vers = [c["version"] for c in self.items]
        self.assertEqual(len(vers), len(set(vers)))

    def test_app_version_matches_history(self):
        hit = next(
            (c for c in self.items if c.get("version") == tabit.APP_VERSION),
            None,
        )
        self.assertIsNotNone(
            hit, f"{tabit.APP_VERSION} missing from release-codenames.json history")
        self.assertEqual(hit["english"], tabit.APP_CODENAME)

    def test_script_title_matches_app(self):
        with open(os.path.join(ROOT, "tabit.py"), encoding="utf-8") as _f:
            text = _f.read()
        ver = re.search(r'^APP_VERSION\s*=\s*"([^"]*)"', text, re.M).group(1)
        code = re.search(r'^APP_CODENAME\s*=\s*"([^"]*)"', text, re.M).group(1)
        self.assertEqual(ver, tabit.APP_VERSION)
        self.assertEqual(code, tabit.APP_CODENAME)


if __name__ == "__main__":
    unittest.main()
