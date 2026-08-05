#!/usr/bin/env python3
"""Golden fixtures for agent_status (tail text → raw / display FSM).

Run from repo root:
  python3 tests/test_agent_status.py

When AI wording changes:
  1. Edit tabit_chrome.toml (and/or agent-detection/*.toml)
  2. Add or update a fixture under tests/fixtures/agent_status/
  3. Re-run this file
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent_status as as_  # noqa: E402

FIXDIR = os.path.join(ROOT, "tests", "fixtures", "agent_status")


def _parse_fixture(path: str):
    """Parse # key: value header, then screen body after # ---."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    meta = {}
    body_lines = []
    in_body = False
    for ln in raw.splitlines():
        if not in_body:
            if ln.strip() == "# ---":
                in_body = True
                continue
            if ln.startswith("#") and ":" in ln:
                key, _, val = ln[1:].partition(":")
                meta[key.strip()] = val.strip()
            continue
        body_lines.append(ln)
    return meta, "\n".join(body_lines)


def _load_all_fixtures():
    cases = []
    if not os.path.isdir(FIXDIR):
        return cases
    for name in sorted(os.listdir(FIXDIR)):
        if not (name.endswith(".txt") or name.endswith(".meta")):
            continue
        path = os.path.join(FIXDIR, name)
        meta, body = _parse_fixture(path)
        if "expect_raw" not in meta:
            continue
        cases.append((name, meta, body))
    return cases


class TestPatternPack(unittest.TestCase):
    def test_loads_toml(self):
        pack = as_.PatternPack.load(
            os.path.join(ROOT, "tabit_chrome.toml"))
        self.assertTrue(pack.source.endswith("tabit_chrome.toml")
                        or pack.source == "builtin")
        self.assertIsNotNone(pack.working.search("Actioning… (5s · ↓ 1k)"))
        self.assertIsNotNone(pack.blocked.search("Do you want to proceed?"))


class TestEvidenceFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        as_.reload_patterns_from_disk()

    def test_fixtures_raw(self):
        cases = _load_all_fixtures()
        self.assertGreaterEqual(len(cases), 5, "expected golden fixtures")
        for name, meta, body in cases:
            with self.subTest(fixture=name):
                osc = meta.get("osc_title", "")
                # map braille escape if written as unicode already
                raw, _ui, _ep, _d = as_.evaluate_screen(
                    body,
                    osc_title=osc,
                    argv=["claude"],
                    selected=True,
                    now=1000.0,
                )
                self.assertEqual(
                    raw, meta["expect_raw"],
                    f"{name}: got {raw!r} want {meta['expect_raw']!r}\n"
                    f"--- body ---\n{body[:400]}",
                )


class TestWorkingEpisode(unittest.TestCase):
    def test_bare_pty_does_not_enter_working(self):
        """PTY activity alone never ignites ▶."""
        ep = as_.Episode(prev_raw="idle", last_pty=1000.0)
        ev = as_.gather_evidence("hello\n❯ typed text")
        raw = as_.detect_raw(ev, ep, now=1000.1, text="hello\n❯ typed text")
        self.assertNotEqual(raw, "working")

    def test_episode_hold_after_chrome(self):
        ep = as_.Episode(
            prev_raw="working", working_since=999.5, last_pty=1000.0)
        # No chrome now, but episode still holding
        ev = as_.gather_evidence("some mid output without chrome")
        raw = as_.detect_raw(
            ev, ep, now=1000.2, text="some mid output without chrome")
        self.assertEqual(raw, "working")

    def test_episode_expires(self):
        ep = as_.Episode(
            prev_raw="working", working_since=900.0, last_pty=900.0)
        ev = as_.gather_evidence("stale screen no chrome")
        raw = as_.detect_raw(
            ev, ep, now=1000.0, text="stale screen no chrome")
        self.assertNotEqual(raw, "working")


class TestDisplayFSM(unittest.TestCase):
    def test_sticky_ready_when_unselected(self):
        disp = as_.Display(ui="working", had_busy_turn=True)
        # enter idle, unselected, wait past READY_STABLE
        d1 = as_.display_step(
            "idle", disp, selected=False, now=10.0, prev_raw="working",
            ready_stable_s=1.0,
        )
        # still within stable window → hold working
        self.assertEqual(d1.ui, "working")
        self.assertGreater(d1.idle_since, 0)
        d2 = as_.display_step(
            "idle", d1, selected=False, now=11.5, prev_raw="idle",
            ready_stable_s=1.0,
        )
        self.assertEqual(d2.ui, "ready")

    def test_selected_clears_to_idle(self):
        disp = as_.Display(
            ui="ready", had_busy_turn=True, idle_since=1.0)
        d = as_.display_step(
            "idle", disp, selected=True, now=20.0, prev_raw="idle",
        )
        self.assertEqual(d.ui, "idle")
        self.assertFalse(d.had_busy_turn)

    def test_no_pause_flash_before_ready(self):
        disp = as_.Display(ui="working", had_busy_turn=True)
        d = as_.display_step(
            "idle", disp, selected=False, now=10.0, prev_raw="working",
            ready_stable_s=1.0,
        )
        self.assertNotEqual(d.ui, "idle")
        self.assertIn(d.ui, ("working", "blocked", "ready"))

    def test_blocked_immediate(self):
        disp = as_.Display(ui="working")
        d = as_.display_step(
            "blocked", disp, selected=False, now=10.0, prev_raw="working",
        )
        self.assertEqual(d.ui, "blocked")
        self.assertTrue(d.had_busy_turn)

    def test_exited_terminal(self):
        disp = as_.Display(ui="working", had_busy_turn=True)
        d = as_.display_step(
            "working", disp, selected=False, now=10.0, dead=True,
        )
        self.assertEqual(d.ui, "exited")

    def test_pty_noise_does_not_promote_ready(self):
        """Regression: idle background tab must stay ⏸ without a busy turn.

        VTE contents-changed (cursor/redraw) used to set unseen_activity and
        promote pause → check while the user did nothing.
        """
        disp = as_.Display(ui="idle", had_busy_turn=False, idle_since=1.0)
        # Spurious PTY callback must not arm sticky ready
        disp = as_.note_unseen_output(disp)
        self.assertFalse(disp.had_busy_turn)
        d = as_.display_step(
            "idle", disp, selected=False, now=100.0, prev_raw="idle",
            ready_stable_s=1.0,
        )
        self.assertEqual(d.ui, "idle")

    def test_select_clears_busy_flag(self):
        d = as_.Display(had_busy_turn=True)
        d = as_.note_tab_selected(d)
        self.assertFalse(d.had_busy_turn)


class TestBlockedBeatsIdlePrompt(unittest.TestCase):
    def test_yes_no_menu_not_idle(self):
        text = (
            "Do you want to proceed?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
        )
        raw, _ui, _ep, _d = as_.evaluate_screen(text, selected=True)
        self.assertEqual(raw, "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
