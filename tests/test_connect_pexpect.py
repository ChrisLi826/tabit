#!/usr/bin/env python3
"""Connect dialog: pexpect pre-check before spawning connect.py."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

from tabit import Tabit  # noqa: E402


class TestConnectPexpectPrecheck(unittest.TestCase):
    def test_has_pexpect_true(self):
        with mock.patch("tabit.subprocess.check_call") as cc:
            cc.return_value = 0
            self.assertTrue(Tabit._ssh_tool_has_pexpect())
            cc.assert_called_once()
            args = cc.call_args[0][0]
            self.assertEqual(args[:2], ["python3", "-c"])
            self.assertIn("import pexpect", args[2])

    def test_has_pexpect_false_on_missing(self):
        with mock.patch(
                "tabit.subprocess.check_call",
                side_effect=FileNotFoundError("python3")):
            self.assertFalse(Tabit._ssh_tool_has_pexpect())

    def test_has_pexpect_false_on_import_fail(self):
        import subprocess
        with mock.patch(
                "tabit.subprocess.check_call",
                side_effect=subprocess.CalledProcessError(1, "python3")):
            self.assertFalse(Tabit._ssh_tool_has_pexpect())

    def test_live_probe_matches_import(self):
        """Sanity: helper agrees with whether this env can import pexpect."""
        try:
            import pexpect  # noqa: F401
            expected = True
        except ImportError:
            expected = False
        # connect.py uses system python3; probe that same interpreter
        import subprocess
        live = subprocess.call(
            [sys.executable, "-c", "import pexpect"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        # Tabit probes `python3` specifically (argv used for connect.py)
        tabit_probe = Tabit._ssh_tool_has_pexpect()
        py3 = subprocess.call(
            ["python3", "-c", "import pexpect"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        self.assertEqual(tabit_probe, py3)
        self.assertEqual(live, expected)


if __name__ == "__main__":
    unittest.main()
