#!/usr/bin/env python3
"""AI agent status: evidence → raw → display (pure, GTK-free).

Architecture (long-term maintainability):

  VTE tail / OSC title
        ↓
  Pattern pack (tabit_chrome.toml) + optional herdr ManifestStore
        ↓
  evidence flags (hard_blocked / hard_working / hard_idle / …)
        ↓
  detect_raw()  — priority ladder + working-episode 1-bit memory
        ↓
  display_step() — small UI FSM (sticky ready, no play→pause→check flicker)

Invariants
----------
1. Detection reads only screen evidence + episode timers; never UI status.
2. Display may read previous UI state; detection must not.
3. Only a working *episode* has memory (chrome ignites, quiet/PTY extinguishes).
   blocked / idle hard flags are pure functions of the current screen.
4. Bare PTY activity never enters working (VTE fires on keystrokes/redraws).
5. AI wording changes belong in tabit_chrome.toml and agent-detection/*.toml,
   not in transition tables.

Wording changes: edit the TOML pattern packs, add a fixture under
tests/fixtures/agent_status/, run tests/test_agent_status.py.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

# vendored tomli
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
try:
    import tomli
except ImportError:  # pragma: no cover
    tomli = None


# ---------------------------------------------------------------------------
# Timers (seconds)
# ---------------------------------------------------------------------------

BUSY_BOTTOM_LINES = 14
TAIL_LINES = 40

# Working episode: chrome ignites; these only *hold* already-working.
WORKING_MIN_HOLD_S = 0.8
# Single quiet window before leaving ▶ (PTY hold + promote quiet merged).
# Formerly PTY_HOLD_S and IDLE_PROMOTE_QUIET_S were both 1.5s with identical
# conditions — one constant is enough.
WORKING_EPISODE_HOLD_S = 1.5
READY_STABLE_S = 1.0


# ---------------------------------------------------------------------------
# Pattern pack (wording surface)
# ---------------------------------------------------------------------------

_DEFAULT_WORKING = [
    r"esc to interrupt",
    r"\bactioning\b",
    r"\brecombobulating\b",
    r"\bthinking…|\bthinking\.\.\.",
    r"\bbaking…|\bbaking\.\.\.",
    r"\brun(?:ning)?\.\.\.",
    r"running tool",
    r"\bgenerating…|\bgenerating\.\.\.",
    r"\bcompacting…|\bcompacting\.\.\.",
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]",
    r"[◐◓◑◒]",
    r"[a-z]{4,}ing\.\.\.\s*\(\d+s\b",
    r"\(\d+s\s*[·•]",
    r"ctrl\+b to run in background",
]

_DEFAULT_BLOCKED = [
    r"do you want to proceed",
    r"do you want to\b",
    r"allow this (action|command|edit|tool)",
    r"permission request",
    r"waiting for (your )?(input|approval|confirmation)\b",
    r"needs? your (input|approval)\b",
    r"press enter to continue",
    r"\[y/n\]|\(y/n\)|\[Y/n\]|\(Y/n\)",
    r"❯\s*1\.\s*Yes",
    r"\b1\.\s*Yes\b.*\b2\.\s*No\b",
    r"run this command\?",
    r"Accept edits\??",
    r"\bAlways allow\b",
    r"Do you want to make this edit",
    # Claude multi-select / form chrome (footer often below list cursor)
    r"enter to select",
    r"tab/arrow keys to navigate",
    r"arrow keys to navigate",
    r"accessing workspace:",
    r"is this a project you created or one you trust",
    r"i trust this folder",
    r"enter to confirm\b",
    r"esc to cancel\b",
    r"no, exit",
]

_DEFAULT_TURN_DONE = [
    r"\bcrunched for\b",
    r"\bsautéed for\b",
    r"\bsauteed for\b",
    r"\bbrewed for\b",
    r"\bbaked for\b",
    r"\brecap:",
]


def _join_or(patterns: List[str]) -> re.Pattern:
    body = "|".join(f"(?:{p})" for p in patterns if p)
    return re.compile(f"(?is){body}")


def _first_re(patterns: List[str], flags: int = re.I | re.S) -> re.Pattern:
    body = "|".join(f"(?:{p})" for p in patterns if p)
    return re.compile(body, flags)


@dataclass
class PatternPack:
    """Compiled VTE chrome patterns. Loaded from tabit_chrome.toml when present."""

    working: re.Pattern
    blocked: re.Pattern
    turn_done: re.Pattern
    trust_confirm: re.Pattern
    trust_choice: re.Pattern
    blocked_strong: re.Pattern
    busy_quick: re.Pattern
    source: str = "builtin"

    @classmethod
    def builtin(cls) -> "PatternPack":
        return cls(
            working=_join_or(_DEFAULT_WORKING),
            blocked=_join_or(_DEFAULT_BLOCKED),
            turn_done=_join_or(_DEFAULT_TURN_DONE),
            trust_confirm=_first_re(
                [r"enter to confirm|esc to cancel|i trust this folder|accessing workspace:"]
            ),
            trust_choice=_first_re(
                [r"(❯|›|>)\s*1\.\s*|^\s*1\.\s*Yes|^\s*2\.\s*No"],
                flags=re.I | re.S | re.M,
            ),
            blocked_strong=_first_re(
                [r"❯\s*1\.\s*Yes|\b1\.\s*Yes\b.*\b2\.\s*No\b|\[y/n\]|"
                 r"do you want to proceed|Accept edits"]
            ),
            busy_quick=_first_re(
                [r"\bactioning\b|\brecombobulating\b|esc to interrupt|"
                 r"[a-z]{4,}ing\.\.\.\s*\(\d+s\b"]
            ),
            source="builtin",
        )

    @classmethod
    def load(cls, path: Optional[str] = None) -> "PatternPack":
        if not path:
            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "tabit_chrome.toml"
            )
        if tomli is None or not path or not os.path.isfile(path):
            return cls.builtin()
        try:
            with open(path, "rb") as f:
                data = tomli.load(f)
        except Exception:
            return cls.builtin()
        if not isinstance(data, dict):
            return cls.builtin()

        def lst(key: str, default: List[str]) -> List[str]:
            v = data.get(key)
            if isinstance(v, list) and v:
                return [str(x) for x in v]
            return default

        pack = cls.builtin()
        try:
            return cls(
                working=_join_or(lst("working", _DEFAULT_WORKING)),
                blocked=_join_or(lst("blocked", _DEFAULT_BLOCKED)),
                turn_done=_join_or(lst("turn_done", _DEFAULT_TURN_DONE)),
                trust_confirm=_first_re(lst("trust_confirm", [
                    r"enter to confirm|esc to cancel|i trust this folder|accessing workspace:"
                ])),
                trust_choice=_first_re(
                    lst("trust_choice", [
                        r"(❯|›|>)\s*1\.\s*|^\s*1\.\s*Yes|^\s*2\.\s*No"
                    ]),
                    flags=re.I | re.S | re.M,
                ),
                blocked_strong=_first_re(lst("blocked_strong", [
                    r"❯\s*1\.\s*Yes|\b1\.\s*Yes\b.*\b2\.\s*No\b|\[y/n\]|"
                    r"do you want to proceed|Accept edits"
                ])),
                busy_quick=_first_re(lst("busy_quick", [
                    r"\bactioning\b|\brecombobulating\b|esc to interrupt|"
                    r"[a-z]{4,}ing\.\.\.\s*\(\d+s\b"
                ])),
                source=path,
            )
        except re.error:
            return pack


# Module-level pack (reloaded by tests if needed)
_PATTERNS: Optional[PatternPack] = None


def get_patterns() -> PatternPack:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = PatternPack.load()
    return _PATTERNS


def set_patterns(pack: Optional[PatternPack]) -> None:
    """Test hook: inject a pack, or None to reload from disk."""
    global _PATTERNS
    _PATTERNS = pack


# ---------------------------------------------------------------------------
# Screen helpers
# ---------------------------------------------------------------------------

def bottom_text(text: str, n: int = BUSY_BOTTOM_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines[-n:])


def last_nonempty_line(text: str) -> str:
    for ln in reversed((text or "").splitlines()):
        if ln.strip():
            return ln
    return ""


def title_busy(osc_title: str) -> bool:
    return bool(osc_title and re.search(r"[\u2800-\u28FF]", osc_title))


# ---------------------------------------------------------------------------
# Evidence (pure function of screen + optional manifest)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    hard_blocked: bool
    hard_working: bool
    hard_idle: bool
    manifest_state: Optional[str] = None
    matched_rule: Optional[str] = None
    flags: Optional[Dict[str, Any]] = None


def bottom_live_busy(text: str, patterns: Optional[PatternPack] = None) -> bool:
    """True if live busy chrome is on the bottom (VTE-visible)."""
    p = patterns or get_patterns()
    if not text or not text.strip():
        return False
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return False
    bottom = "\n".join(lines[-BUSY_BOTTOM_LINES:])
    last = next((ln for ln in reversed(lines) if ln.strip()), "")
    if p.turn_done.search(bottom) and not p.busy_quick.search(bottom):
        return False
    if re.match(r"^\s*[❯›>]\s*$", last) or re.match(r"^\s*[❯›]\s+\S", last):
        if not p.busy_quick.search(bottom):
            return False
    return bool(p.working.search(bottom))


def hard_idle_prompt(text: str, patterns: Optional[PatternPack] = None) -> bool:
    """Clear idle prompt at end, with no live busy chrome in bottom."""
    p = patterns or get_patterns()
    if not text or not text.strip():
        return False
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return False
    bottom = "\n".join(lines[-BUSY_BOTTOM_LINES:])
    last = next((ln for ln in reversed(lines) if ln.strip()), "")
    prompt = bool(
        re.match(r"^\s*[❯›>]\s*$", last)
        or re.match(r"^\s*[❯›]\s+\S", last)
        or re.match(r"^\s*Human:\s*$", last)
    )
    if not prompt:
        return False
    if p.working.search(bottom):
        return False
    return True


def bottom_blocked(text: str, patterns: Optional[PatternPack] = None) -> bool:
    p = patterns or get_patterns()
    if not text:
        return False
    bottom = bottom_text(text)
    if p.blocked.search(bottom):
        # trust / confirm menus use "› 1." not only "❯ 1. Yes"
        if p.trust_confirm.search(bottom) and p.trust_choice.search(bottom):
            return True
        return True
    if p.trust_confirm.search(bottom) and p.trust_choice.search(bottom):
        return True
    return False


def gather_evidence(
    text: str,
    *,
    osc_title: str = "",
    manifest_state: Optional[str] = None,
    matched_rule: Optional[str] = None,
    flags: Optional[Dict[str, Any]] = None,
    patterns: Optional[PatternPack] = None,
) -> Evidence:
    """Build hard flags from screen + optional herdr evaluate() result.

    Detection does not call ManifestStore itself so tests stay free of I/O.
    """
    p = patterns or get_patterns()
    flags = flags or {}
    live_busy = bottom_live_busy(text, p)
    t_busy = title_busy(osc_title)

    hard_working = bool(
        manifest_state == "working"
        or flags.get("visible_working")
        or live_busy
        or t_busy
    )
    hard_blocked = bool(
        manifest_state == "blocked"
        or flags.get("visible_blocker")
        or bottom_blocked(text, p)
    )
    hard_idle = hard_idle_prompt(text, p) and not hard_blocked
    if manifest_state == "idle" and matched_rule and hard_idle:
        hard_idle = True
    if p.turn_done.search(text or "") and not live_busy:
        bottom = bottom_text(text)
        if p.turn_done.search(bottom) and not live_busy:
            hard_idle = True

    return Evidence(
        hard_blocked=hard_blocked,
        hard_working=hard_working,
        hard_idle=hard_idle,
        manifest_state=manifest_state,
        matched_rule=matched_rule,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Detection: evidence + working-episode memory → raw
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    """Detection memory: only the working episode has state."""

    prev_raw: Optional[str] = None
    working_since: float = 0.0
    last_pty: float = 0.0


def working_episode_active(ep: Episode, now: float) -> bool:
    """True if we should keep raw=working after chrome has left.

    Chrome ignites working elsewhere; this only *holds* an episode.
    Merges former min-hold, PTY-hold, and quiet-promote branches.
    """
    if ep.prev_raw != "working":
        return False
    if ep.working_since and (now - ep.working_since) < WORKING_MIN_HOLD_S:
        return True
    last = float(ep.last_pty or 0)
    pty_age = (now - last) if last else 1e9
    if pty_age < WORKING_EPISODE_HOLD_S:
        return True
    return False


def detect_raw(
    evidence: Evidence,
    episode: Episode,
    now: float,
    *,
    patterns: Optional[PatternPack] = None,
    argv: Optional[list] = None,
    text: str = "",
) -> str:
    """Map evidence → raw status.

    Priority: blocked → hard working → hard idle → working episode hold
    → manifest state → heuristic fallback.

    Returns: working | idle | blocked | unknown
    """
    if evidence.hard_blocked:
        return "blocked"
    if evidence.hard_working:
        return "working"
    if evidence.hard_idle:
        return "idle"
    if working_episode_active(episode, now):
        return "working"

    state = evidence.manifest_state
    if state == "working":
        return "working"
    if state in ("idle", "blocked", "unknown"):
        return "idle" if state != "blocked" else "blocked"

    return heuristic_fallback(text, argv=argv, patterns=patterns)


def heuristic_fallback(
    text: str,
    argv: Optional[list] = None,
    patterns: Optional[PatternPack] = None,
) -> str:
    """Bottom-of-screen heuristics when no hard flags / no useful manifest."""
    p = patterns or get_patterns()
    if not text or not text.strip():
        return "unknown"
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "unknown"

    bottom = "\n".join(lines[-16:])
    last = next((ln for ln in reversed(lines) if ln.strip()), "")

    is_blocked = bool(p.blocked.search(bottom))
    is_working = bool(p.working.search(bottom))
    turn_done = bool(p.turn_done.search(bottom))

    prompt_idle = bool(
        re.match(r"^\s*[❯›$%#]\s*$", last)
        or re.match(r"^\s*[❯›]\s+\S", last)
        or re.match(r"^\s*Human:\s*$", last)
    )

    if is_blocked and p.blocked_strong.search(bottom):
        return "blocked"
    if is_blocked and not is_working and not prompt_idle:
        return "blocked"

    if turn_done and not re.search(
        r"(?is)\bactioning\b|esc to interrupt", bottom
    ):
        return "idle"
    if prompt_idle and not re.search(
        r"(?is)\bactioning\b|esc to interrupt|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]",
        bottom,
    ):
        return "idle"

    if is_working and not prompt_idle:
        return "working"
    if is_working and prompt_idle:
        return "idle"

    idle_re = re.compile(
        r"(?m)"
        r"(?:^|\n)\s*[❯›]\s*$"
        r"|(?:^|\n)\s*[❯›]\s+"
        r"|(?:^|\n)Human:\s*$"
        r"|(?:^|\n)>\s*$"
        r"|(?:^|\n)\s*$"
    )
    if idle_re.search("\n".join(lines[-8:])):
        return "idle"
    if prompt_idle:
        return "idle"

    if argv:
        blob = " ".join(str(a) for a in argv).lower()
        for name in ("claude", "codex", "grok", "cursor", "opencode",
                     "gemini", "agy", "pi ", "copilot"):
            if name.strip() in blob:
                return "idle"
    return "unknown"


def apply_raw_to_episode(ep: Episode, raw: str, now: float) -> Episode:
    """Update episode bookkeeping after a detect_raw result."""
    out = replace(ep)
    if raw == "working":
        if ep.prev_raw != "working":
            out.working_since = now
    out.prev_raw = raw
    return out


# ---------------------------------------------------------------------------
# Display mini-FSM
# ---------------------------------------------------------------------------
# States: working | blocked | idle | ready | exited | unknown
#
# Sticky ✔ only after a real busy turn (working/blocked) while the tab was
# not reviewed. Bare PTY noise (cursor blink, redraw, keystrokes on another
# session) must NOT promote idle → ready — that was pause→check with no work.
#
# Events:
#   raw=working|blocked → set had_busy_turn, show ▶/?
#   raw=idle + unselected + had_busy_turn + stable → ✔
#   tab_selected → clear had_busy_turn, ✔→⏸
#   dead → exited
# ---------------------------------------------------------------------------

RAW_STATES = frozenset({"working", "idle", "blocked", "unknown", "exited"})
UI_STATES = frozenset({
    "working", "idle", "blocked", "ready", "exited", "unknown", "done",
})


@dataclass
class Display:
    """UI status machine state."""

    ui: Optional[str] = None
    # True only after raw working/blocked since last time user viewed the tab.
    # NOT set by bare PTY contents-changed (that caused idle→ready spam).
    had_busy_turn: bool = False
    idle_since: float = 0.0


def note_unseen_output(disp: Display) -> Display:
    """PTY output on an unselected tab — does not qualify for sticky ✔.

    Kept as a no-op so callers can stay; ready requires had_busy_turn only.
    """
    return disp


def note_tab_selected(disp: Display) -> Display:
    """User opened the AI tab — clear sticky bookkeeping."""
    return replace(disp, had_busy_turn=False)


def display_step(
    raw: str,
    disp: Display,
    *,
    selected: bool,
    now: float,
    ready_stable_s: float = READY_STABLE_S,
    dead: bool = False,
    prev_raw: Optional[str] = None,
) -> Display:
    """One step of the display FSM.

    Pure: returns a new Display. Caller applies .ui to widgets.
    """
    if dead:
        return Display(ui="exited", had_busy_turn=False, idle_since=0.0)

    d = replace(disp)
    prev_ui = d.ui

    if raw == "working":
        d.had_busy_turn = True
        d.idle_since = 0.0
        d.ui = "working"
        return d

    if raw == "blocked":
        d.had_busy_turn = True
        d.idle_since = 0.0
        d.ui = "blocked"
        return d

    if raw == "idle":
        if prev_raw != "idle":
            d.idle_since = now
        idle_for = (now - float(d.idle_since)) if d.idle_since else 0.0
        # Real busy episode only — not PTY redraw noise.
        want_ready = bool(
            d.had_busy_turn
            or prev_ui in ("working", "ready", "blocked")
        )
        if selected:
            d.had_busy_turn = False
            d.ui = "idle"
            return d
        if want_ready and idle_for >= ready_stable_s:
            d.ui = "ready"
            return d
        if prev_ui == "ready":
            d.ui = "ready"
            return d
        if want_ready:
            # Hold ▶/? until ready fires — no brief ⏸ flash
            d.ui = (
                prev_ui if prev_ui in ("working", "blocked", "ready")
                else "working"
            )
            return d
        d.ui = "idle"
        return d

    # raw unknown / other
    if prev_ui == "ready" and not selected:
        d.ui = "ready"
        return d
    if raw == "unknown":
        d.ui = prev_ui or "unknown"
        d.idle_since = 0.0
        return d
    d.ui = raw
    d.idle_since = 0.0
    return d


# ---------------------------------------------------------------------------
# High-level one-shot (fixtures / tests)
# ---------------------------------------------------------------------------

def evaluate_screen(
    text: str,
    *,
    osc_title: str = "",
    argv: Optional[list] = None,
    episode: Optional[Episode] = None,
    display: Optional[Display] = None,
    selected: bool = True,
    now: float = 1000.0,
    manifest_state: Optional[str] = None,
    matched_rule: Optional[str] = None,
    flags: Optional[dict] = None,
    patterns: Optional[PatternPack] = None,
    dead: bool = False,
) -> Tuple[str, str, Episode, Display]:
    """Full pipeline for tests: screen → (raw, ui, episode, display)."""
    ep = episode or Episode()
    disp = display or Display()
    if dead:
        disp = display_step(
            "exited", disp, selected=selected, now=now, dead=True,
            prev_raw=ep.prev_raw,
        )
        ep = replace(ep, prev_raw="exited")
        return "exited", disp.ui or "exited", ep, disp

    ev = gather_evidence(
        text,
        osc_title=osc_title,
        manifest_state=manifest_state,
        matched_rule=matched_rule,
        flags=flags,
        patterns=patterns,
    )
    raw = detect_raw(
        ev, ep, now, patterns=patterns, argv=argv, text=text,
    )
    prev_raw = ep.prev_raw
    ep = apply_raw_to_episode(ep, raw, now)
    disp = display_step(
        raw, disp, selected=selected, now=now, prev_raw=prev_raw, dead=False,
    )
    return raw, disp.ui or "unknown", ep, disp


def find_chrome_toml() -> Optional[str]:
    """Locate tabit_chrome.toml next to this module or under share dirs."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "tabit_chrome.toml"),
        os.path.expanduser("~/.local/share/tabit/tabit_chrome.toml"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def reload_patterns_from_disk() -> PatternPack:
    path = find_chrome_toml()
    pack = PatternPack.load(path)
    set_patterns(pack)
    return pack
