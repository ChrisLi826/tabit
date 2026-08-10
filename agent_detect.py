#!/usr/bin/env python3
"""Agent screen-status detection using herdr-compatible TOML manifests.

No herdr binary required. Manifests live in agent-detection/ (vendored from
https://github.com/herdrdev/herdr and https://herdr.dev/agent-detection/).

States: idle | working | blocked | unknown
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# vendored tomli (Python 3.10)
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
try:
    import tomli
except ImportError:  # pragma: no cover
    tomli = None

# Remote update (optional; offline vendored copy always works)
MANIFEST_INDEX_URL = "https://herdr.dev/agent-detection/index.toml"
MANIFEST_BASE_URL = "https://herdr.dev/agent-detection"
GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/herdrdev/herdr/master/"
    "website/agent-detection"
)
# Bundled manifests are a few KB; refuse anything wildly bigger.
MAX_MANIFEST_BYTES = 512 * 1024

# herdr regex dialect → Python
_HEX_ESC = re.compile(r"\\x\{([0-9A-Fa-f]+)\}")


def _herdr_regex_to_python(pattern: str) -> str:
    def repl(m: re.Match) -> str:
        h = m.group(1)
        if len(h) <= 4:
            return "\\u" + h.zfill(4)
        return "\\U" + h.zfill(8)

    return _HEX_ESC.sub(repl, pattern)


def _compile_re(pattern: str) -> Optional[re.Pattern]:
    try:
        return re.compile(_herdr_regex_to_python(pattern), re.MULTILINE)
    except re.error:
        try:
            return re.compile(pattern, re.MULTILINE)
        except re.error:
            return None


def _version_key(value: Any) -> list:
    """Sort key for manifest versions like 2026.07.13.1.

    Compare component by component, numbers as numbers: plain string compare
    puts "2026.07.13.10" before ".9", and breaks entirely if upstream ever
    stops zero-padding the date.
    """
    out = []
    for part in str(value or "").split("."):
        try:
            out.append((0, int(part), ""))
        except ValueError:
            out.append((1, 0, part))
    return out


def _non_empty_lines(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def extract_region(
    text: str,
    region: str,
    *,
    osc_title: str = "",
    osc_progress: str = "",
) -> str:
    """Subset of herdr regions used by bundled manifests.

    osc_title / osc_progress come from the terminal emulator (VTE window
    title tracks OSC 0/2; progress is best-effort if available).
    """
    region = (region or "whole_recent").strip()

    if region == "osc_title":
        return osc_title or ""
    if region == "osc_progress":
        return osc_progress or ""

    if not text:
        return ""
    lines = text.splitlines()
    ne = _non_empty_lines(text)

    if region in ("whole_recent", "whole"):
        return text

    m = re.fullmatch(r"bottom_non_empty_lines\((\d+)\)", region)
    if m:
        n = int(m.group(1))
        return "\n".join(ne[-n:]) + ("\n" if ne else "")

    if region == "after_last_horizontal_rule":
        # horizontal rules: ─── or --- or ━━━
        idx = -1
        for i, ln in enumerate(lines):
            s = ln.strip()
            if len(s) >= 3 and (
                set(s) <= {"─", "━", "-", "—", "–"} or re.fullmatch(r"[-─━—–]{3,}", s)
            ):
                idx = i
        if idx >= 0:
            return "\n".join(lines[idx + 1 :])
        return text

    if region == "prompt_box_body":
        # Idle prompt box only — not "❯ 1. Yes" menu rows (herdr leaves
        # prompt_box_body empty in that case so higher blocked rules win).
        start = None
        for i, ln in enumerate(lines):
            if re.match(r"^\s*❯", ln) and not re.match(r"^\s*❯\s*\d+\.", ln):
                start = i
        if start is None:
            return ""
        return "\n".join(lines[start:])

    # unknown region: whole text
    return text


def _as_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]


def _matcher_ok(region_text: str, matcher: Any) -> bool:
    """Evaluate one matcher table (AND of its fields; nested any/all/not)."""
    if matcher is None:
        return True
    if not isinstance(matcher, dict):
        return False

    low = region_text.lower()
    lines = region_text.splitlines() or [""]

    if "contains" in matcher:
        for s in _as_list(matcher["contains"]):
            if str(s).lower() not in low:
                return False

    if "line_regex" in matcher:
        ok = False
        for p in _as_list(matcher["line_regex"]):
            cre = _compile_re(str(p))
            if cre and any(cre.search(ln) for ln in lines):
                ok = True
                break
        if not ok:
            return False

    if "regex" in matcher:
        ok = False
        for p in _as_list(matcher["regex"]):
            cre = _compile_re(str(p))
            if cre and cre.search(region_text):
                ok = True
                break
        if not ok:
            return False

    if "any" in matcher:
        if not any(_matcher_ok(region_text, m) for m in _as_list(matcher["any"])):
            return False

    if "all" in matcher:
        if not all(_matcher_ok(region_text, m) for m in _as_list(matcher["all"])):
            return False

    if "not" in matcher:
        for m in _as_list(matcher["not"]):
            if _matcher_ok(region_text, m):
                return False

    return True


def _rule_matches(region_text: str, rule: dict) -> bool:
    """Top-level rule matchers (same shape as a matcher table)."""
    keys = set(rule.keys()) - {
        "id", "state", "priority", "region", "visible_working",
        "visible_blocker", "visible_idle", "skip_state_update",
    }
    if not keys:
        return False
    # reuse matcher logic on a filtered dict of only match keys
    match_dict = {k: rule[k] for k in keys}
    return _matcher_ok(region_text, match_dict)


def evaluate_manifest(
    screen: str,
    manifest: dict,
    *,
    osc_title: str = "",
    osc_progress: str = "",
) -> Tuple[str, Optional[str], dict]:
    """Return (state, matched_rule_id, flags).

    flags: visible_idle / visible_blocker / visible_working (herdr metadata).
    """
    rules = list(manifest.get("rules") or [])
    rules.sort(key=lambda r: int(r.get("priority") or 0), reverse=True)
    flags = {
        "visible_idle": False,
        "visible_blocker": False,
        "visible_working": False,
        "skip_state_update": False,
    }

    for rule in rules:
        region = extract_region(
            screen,
            rule.get("region") or "whole_recent",
            osc_title=osc_title,
            osc_progress=osc_progress,
        )
        if not _rule_matches(region, rule):
            continue
        if rule.get("visible_idle"):
            flags["visible_idle"] = True
        if rule.get("visible_blocker"):
            flags["visible_blocker"] = True
        if rule.get("visible_working"):
            flags["visible_working"] = True
        if rule.get("skip_state_update"):
            flags["skip_state_update"] = True
            continue  # matched special UI; don't set state from this rule
        state = (rule.get("state") or "unknown").strip().lower()
        if state not in ("idle", "working", "blocked", "unknown"):
            state = "unknown"
        return state, rule.get("id"), flags

    # herdr: known agent, no rule → idle fallback
    return "idle", None, flags


class ManifestStore:
    """Load / cache / optional remote update of herdr agent-detection TOMLs."""

    def __init__(
        self,
        vendored_dir: str,
        cache_dir: str,
        check_interval_sec: int = 24 * 3600,
        remote_check: bool = True,
    ):
        self.vendored_dir = vendored_dir
        self.cache_dir = cache_dir
        self.check_interval_sec = check_interval_sec
        self.remote_check = remote_check
        self._manifests: Dict[str, dict] = {}
        self._mtime: Dict[str, float] = {}
        self._aliases: Dict[str, str] = {}
        self._meta_path = os.path.join(cache_dir, "update_meta.json")
        self._load_all()

    def _candidate_dirs(self) -> List[str]:
        dirs = []
        if self.cache_dir and os.path.isdir(self.cache_dir):
            dirs.append(self.cache_dir)
        if self.vendored_dir and os.path.isdir(self.vendored_dir):
            dirs.append(self.vendored_dir)
        return dirs

    def _load_file(self, path: str) -> Optional[dict]:
        if tomli is None:
            return None
        try:
            with open(path, "rb") as f:
                data = tomli.load(f)
            if isinstance(data, dict) and data.get("id"):
                return data
        except (OSError, ValueError, tomli.TOMLDecodeError if tomli else Exception):
            pass
        return None

    def _load_all(self) -> None:
        self._manifests.clear()
        self._aliases.clear()
        # vendored first, then cache overwrites if newer version
        for d in reversed(self._candidate_dirs()):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".toml") or name == "index.toml":
                    continue
                path = os.path.join(d, name)
                data = self._load_file(path)
                if not data:
                    continue
                mid = str(data.get("id") or name[:-5])
                # prefer higher version string when both exist
                prev = self._manifests.get(mid)
                if prev is None or (_version_key(data.get("version"))
                                    >= _version_key(prev.get("version"))):
                    self._manifests[mid] = data
                    self._mtime[mid] = os.path.getmtime(path)
                for a in data.get("aliases") or []:
                    self._aliases[str(a).lower()] = mid
                self._aliases[mid.lower()] = mid

    def resolve_agent(self, kind: str) -> Optional[str]:
        if not kind:
            return None
        k = kind.lower().strip()
        return self._aliases.get(k) or (k if k in self._manifests else None)

    def evaluate(
        self,
        screen: str,
        agent_kind: str,
        *,
        osc_title: str = "",
        osc_progress: str = "",
    ) -> Tuple[str, Optional[str], str, dict]:
        """Return (state, rule_id, source_agent_id, flags)."""
        mid = self.resolve_agent(agent_kind) or self.resolve_agent("claude")
        if not mid or mid not in self._manifests:
            return "unknown", None, agent_kind or "", {}
        state, rid, flags = evaluate_manifest(
            screen,
            self._manifests[mid],
            osc_title=osc_title,
            osc_progress=osc_progress,
        )
        return state, rid, mid, flags

    def maybe_update_remote(self, force: bool = False) -> str:
        """Fetch newer manifests from herdr.dev into cache_dir. Returns status string."""
        if not self.remote_check and not force:
            return "disabled"
        os.makedirs(self.cache_dir, exist_ok=True)
        meta = {}
        try:
            with open(self._meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        now = time.time()
        last = float(meta.get("last_check", 0))
        if not force and (now - last) < self.check_interval_sec:
            return "skipped_recent"

        def fetch(url: str, timeout: int = 12) -> Optional[bytes]:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "tabit-agent-detect"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    # Cap the body: a manifest is a few KB, and this text ends
                    # up compiled into regexes on the UI thread.
                    body = resp.read(MAX_MANIFEST_BYTES + 1)
                    if len(body) > MAX_MANIFEST_BYTES:
                        return None
                    return body
            except Exception:
                return None

        raw = fetch(MANIFEST_INDEX_URL) or fetch(f"{GITHUB_RAW_BASE}/index.toml")
        if not raw:
            meta["last_check"] = now
            meta["last_result"] = "network_error"
            try:
                with open(self._meta_path, "w") as f:
                    json.dump(meta, f, indent=2)
            except OSError:
                pass
            return "network_error"

        if tomli is None:
            return "no_tomli"
        try:
            index = tomli.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return "bad_index"

        # index: [[agents]] id/path entries
        agents = []
        if isinstance(index, dict):
            if isinstance(index.get("agents"), list):
                agents = index["agents"]
            if not agents:
                agents = list(self._manifests.keys()) or [
                    "claude", "codex", "grok", "cursor", "gemini", "opencode",
                ]
        # list of (id, filename)
        files = []
        for a in agents:
            if isinstance(a, str):
                files.append((a.replace(".toml", ""), a if a.endswith(".toml") else f"{a}.toml"))
            elif isinstance(a, dict) and a.get("id"):
                aid = str(a["id"])
                files.append((aid, str(a.get("path") or f"{aid}.toml")))

        updated = 0
        for aid, fname in files:
            body = fetch(f"{MANIFEST_BASE_URL}/{fname}") or fetch(
                f"{GITHUB_RAW_BASE}/{fname}"
            )
            if not body:
                continue
            try:
                data = tomli.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(data, dict) or not data.get("id"):
                continue
            mid = str(data.get("id") or aid)
            path = os.path.join(self.cache_dir, fname if fname.endswith(".toml") else f"{mid}.toml")
            old = self._manifests.get(mid)
            if old is None or (_version_key(data.get("version"))
                               >= _version_key(old.get("version"))):
                try:
                    with open(path, "wb") as f:
                        f.write(body)
                    updated += 1
                except OSError:
                    pass

        self._load_all()
        meta["last_check"] = now
        meta["last_result"] = f"ok_updated_{updated}"
        try:
            with open(self._meta_path, "w") as f:
                json.dump(meta, f, indent=2)
        except OSError:
            pass
        return meta["last_result"]


# --- tests -----------------------------------------------------------------
if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    store = ManifestStore(
        vendored_dir=os.path.join(here, "agent-detection"),
        cache_dir=os.path.join(here, "agent-detection"),
        remote_check=False,
    )
    cases = [
        ("idle", "claude", "※ Crunched for 1m\n\n❯ \n"),
        ("block", "claude", "Bash command\ndeploy\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"),
        ("working word", "claude", "playback confirmed working\n\n❯ \n"),
    ]
    for name, kind, screen in cases:
        st, rid, mid, flags = store.evaluate(screen, kind)
        print(f"{name:15} -> {st:10} rule={rid} agent={mid} flags={flags}")
