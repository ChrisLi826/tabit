#!/usr/bin/env python3
"""Phone new-tab: Serial, Connect when this machine has it, no Command."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402

PAGE = os.path.join(ROOT, "remote.html")


def _fields(**over):
    base = {
        "sn": "AbC", "env": "prod", "email": "a@b.c", "password": "pw",
        "ticket": "", "org": "", "network": "", "device_type": "auto",
        "no_cache": False, "use_tmux": True, "reconnect": True,
        "gateway_pass": "", "magic_words": "", "agent": "",
    }
    base.update(over)
    return base


class TestSerialSpec(unittest.TestCase):
    def test_screen_uses_the_default_baud(self):
        spec = tabit.Tabit._serial_spec("screen", "/dev/ttyUSB0", "")
        self.assertEqual(spec["argv"], tabit.Tabit._serial_argv(
            "screen", "/dev/ttyUSB0", "115200"))
        self.assertEqual(spec["label"], "ttyUSB0")
        self.assertEqual(spec["sub"], "115200")
        self.assertEqual(spec["icon"], "network-wired-symbolic")

    def test_other_tool_names_itself(self):
        spec = tabit.Tabit._serial_spec("picocom", "/dev/ttyACM0", "9600")
        self.assertEqual(spec["sub"], "picocom:9600")
        self.assertEqual(spec["argv"][0], "picocom")

    def test_ssh_and_telnet_take_a_host(self):
        ssh = tabit.Tabit._serial_spec("ssh", "user@host", "2222")
        self.assertEqual(ssh["argv"], ["ssh", "-p", "2222", "user@host"])
        self.assertEqual(ssh["sub"], "ssh:2222")
        tel = tabit.Tabit._serial_spec("telnet", "host", "")
        self.assertEqual(tel["argv"], ["telnet", "host"])
        self.assertEqual(tel["sub"], "telnet")

    def test_bad_input(self):
        self.assertEqual(tabit.Tabit._serial_spec("nope", "/dev/ttyUSB0", ""),
                         "pick a serial tool")
        self.assertEqual(tabit.Tabit._serial_spec("screen", "  ", ""),
                         "give a device path")
        self.assertEqual(tabit.Tabit._serial_spec("ssh", "", "22"), "give a host")
        self.assertIsInstance(tabit.Tabit._serial_spec("screen", "x" * 501, ""), str)


class TestConnectPlan(unittest.TestCase):
    def test_tmux_argv_does_not_kill(self):
        spec = tabit.Tabit._connect_launch(_fields())
        self.assertEqual(spec["argv"][:5], ["tmux", "new-session", "-A", "-s", "conn-abc"])
        self.assertNotIn("kill", " ".join(spec["argv"]))
        self.assertNotIn("--env", spec["raw"])
        self.assertIn("--password", spec["raw"])
        self.assertEqual(spec["icon"], tabit.ICON_CONNECT_TMUX)

    def test_flags_and_agent_session(self):
        spec = tabit.Tabit._connect_launch(_fields(
            env="lab", ticket="t", org="o", network="n", device_type="ap",
            no_cache=True, gateway_pass="g", magic_words="m", agent="AA:BB.cc"))
        raw = spec["raw"]
        self.assertEqual(spec["session"], "conn-abc-aabb-cc")
        for bit in ("--env", "lab", "--ticket", "--org", "--network",
                    "--device-type", "ap", "--no-cache", "--gateway-pass",
                    "--magic-words", "--agent", "AA:BB.cc"):
            self.assertIn(bit, raw)
        self.assertEqual(spec["label"], "AbC → AA:BB.cc (connect)")
        self.assertNotIn("reconnect", spec["argv"])

    def test_no_tmux(self):
        spec = tabit.Tabit._connect_launch(_fields(use_tmux=False, env="lab"))
        self.assertIsNone(spec["session"])
        self.assertEqual(spec["argv"][0], "python3")
        self.assertEqual(spec["icon"], tabit.ICON_CONNECT)
        self.assertEqual(spec["sub"], "cloud (lab)")

    def test_remember_skips_agent_and_reconnect(self):
        remembered = tabit.Tabit._connect_remember(_fields(agent="x", reconnect=True))
        self.assertNotIn("agent", remembered)
        self.assertNotIn("reconnect", remembered)
        self.assertEqual(remembered["password"], "pw")

    def test_blank_secret_keeps_the_saved_one(self):
        last = {"password": "saved-pw", "ticket": "saved-t", "gateway_pass": "saved-g",
                "email": "old@b.c"}
        got = tabit.Tabit._connect_phone_values(
            {"sn": "NEW", "email": "new@b.c", "password": ""}, last)
        self.assertEqual(got["password"], "saved-pw")
        self.assertEqual(got["ticket"], "saved-t")
        self.assertEqual(got["gateway_pass"], "saved-g")
        self.assertEqual(got["email"], "new@b.c")
        self.assertTrue(got["use_tmux"])
        self.assertFalse(got["reconnect"])

    def test_typed_secret_replaces_the_saved_one(self):
        got = tabit.Tabit._connect_phone_values(
            {"sn": "N", "password": "new-pw"}, {"password": "saved-pw"})
        self.assertEqual(got["password"], "new-pw")

    def test_public_form_has_no_secret_text(self):
        form = tabit.Tabit._connect_public({
            "sn": "S", "password": "sekret-token-xyz", "ticket": "tick-token",
            "gateway_pass": "gw-token", "email": "a@b.c", "env": "lab",
            "device_type": "camera", "use_tmux": False, "no_cache": True,
            "org": "org1", "magic_words": "one,two",
        }, ["prod", "lab"])
        blob = json.dumps(form)
        self.assertNotIn("sekret-token-xyz", blob)
        self.assertNotIn("tick-token", blob)
        self.assertNotIn("gw-token", blob)
        self.assertNotIn("password", form)
        self.assertNotIn("ticket", form)
        self.assertNotIn("gateway_pass", form)
        self.assertTrue(form["saved_password"])
        self.assertTrue(form["saved_ticket"])
        self.assertTrue(form["saved_gateway_pass"])
        self.assertEqual(form["email"], "a@b.c")
        self.assertEqual(form["env"], "lab")
        self.assertEqual(form["device_type"], "camera")
        self.assertFalse(form["use_tmux"])

    def test_empty_sn_and_bad_type(self):
        with self.assertRaises(ValueError):
            tabit.Tabit._connect_phone_values({"sn": "  "}, {})
        with self.assertRaises(ValueError):
            tabit.Tabit._connect_phone_values({"sn": "A", "device_type": "router"}, {})


class TestNewTabPage(unittest.TestCase):
    def test_the_sheet_offers_serial_and_connect(self):
        with open(PAGE, encoding="utf-8") as f:
            src = f.read()
        body = src.split("function newTab", 1)[1].split("// ai-name:start", 1)[0]
        self.assertNotIn('data-k="command"', body)
        self.assertNotIn("forcmd", body)
        self.assertIn('data-k="serial"', body)
        self.assertIn('data-k="connect"', body)
        self.assertIn("lastSnap.connect", body)
        self.assertIn('class="forfolder"', body)
        self.assertIn("api/new-options", body)
        self.assertIn("if (pwd) body.password = pwd", body)
        self.assertIn("if (ticket) body.ticket = ticket", body)
        self.assertIn("if (gw) body.gateway_pass = gw", body)
        self.assertIn("else clearAiName()", body)
        self.assertIn('id="nhid"', body)

    def test_server_still_accepts_a_cached_command(self):
        with open(os.path.join(ROOT, "tabit.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('elif kind == "command"', src)
        self.assertIn('"kind must be shell, ai, serial or connect"', src)


if __name__ == "__main__":
    unittest.main()
