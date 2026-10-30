#!/usr/bin/env python3
"""Write one tab's Grok status where tabit already reads Claude's.

Grok runs this on a hook. Stdin is one JSON object. The tab key is
TABIT_AGENT, or the tmux session when TMUX_PANE is set. The file is five
lines: state, transcript, size, an empty field, and the newest prompt id.
A child agent's event (subagentType) does not change the parent. A report
for an older prompt does not cover a newer turn. Exit 0 always: a Stop
hook that fails or exits 2 would keep Grok working.
"""

import json
import os
import subprocess
import sys

try:
    import fcntl
except ImportError:  # pragma: no cover - tabit runs on Linux
    fcntl = None

_STATES = ("working", "blocked", "idle", "background")
_SETTLE = ("Stop", "StopFailure", "StopCancelled")
_WORK = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
_EVENTS = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "post_tool_use_failure": "PostToolUseFailure",
    "stop": "Stop",
    "stop_failure": "StopFailure",
    "stop_cancelled": "StopCancelled",
    "notification": "Notification",
}


def event_name(payload):
    name = payload.get("hook_event_name") or ""
    if isinstance(name, str) and name:
        return name
    snake = payload.get("hookEventName") or ""
    if not isinstance(snake, str):
        return ""
    return _EVENTS.get(snake, snake)


def _note(payload):
    value = payload.get("notification_type")
    if value is None:
        value = payload.get("notificationType")
    return value if isinstance(value, str) else ""


def _prompt_id(payload):
    value = payload.get("promptId")
    if value is None:
        value = payload.get("prompt_id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).replace("\n", "").replace("\r", "")


def _subagent(payload):
    value = payload.get("subagentType")
    if value is None:
        value = payload.get("subagent_type")
    return isinstance(value, str) and bool(value.strip())


def _running_background(payload):
    tasks = payload.get("backgroundTasks")
    if not isinstance(tasks, list):
        tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if isinstance(task, dict) and task.get("status") == "running":
            return True
    return False


def decide(event, payload, stored_id):
    """(state, prompt id) to write, or None to leave the file alone."""
    if _subagent(payload):
        return None
    pid = _prompt_id(payload)
    stored_id = stored_id or ""
    if event == "SessionStart":
        return "idle", ""
    if event == "UserPromptSubmit":
        return "working", pid
    if event in _WORK:
        return "working", pid or stored_id
    if event == "Notification" and _note(payload) == "permission_prompt":
        return "blocked", pid or stored_id
    settle = event in _SETTLE or (
        event == "Notification" and _note(payload) == "idle_prompt")
    if not settle:
        return None
    # A cancel for the previous turn can arrive after the next prompt.
    if pid and stored_id and pid != stored_id:
        return None
    state = "background" if _running_background(payload) else "idle"
    return state, pid or stored_id


def _safe_key(key):
    return bool(key) and key not in (".", "..") and "/" not in key and "\0" not in key


def _read_prompt_id(path):
    try:
        with open(path) as handle:
            lines = handle.read().split("\n")
    except OSError:
        return ""
    if len(lines) > 4:
        return lines[4]
    return ""


def _transcript(payload):
    value = payload.get("transcript_path")
    if value is None:
        value = payload.get("transcriptPath")
    if not isinstance(value, str):
        return ""
    return value.split("\n", 1)[0].replace("\r", "")


def _write_state(dest, state, payload, prompt_id):
    if state not in _STATES:
        return
    transcript = _transcript(payload)
    try:
        size = str(os.stat(transcript).st_size) if transcript else ""
    except OSError:
        size = ""
    body = "%s\n%s\n%s\n\n%s\n" % (state, transcript, size, prompt_id)
    tmp = dest + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, dest)


def apply(hook_dir, key, payload):
    if not _safe_key(key) or not isinstance(payload, dict):
        return
    os.makedirs(hook_dir, mode=0o700, exist_ok=True)
    dest = os.path.join(hook_dir, key)
    lock_path = dest + ".lock"
    lock = open(lock_path, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        decided = decide(event_name(payload), payload, _read_prompt_id(dest))
        if decided is not None:
            _write_state(dest, decided[0], payload, decided[1])
    finally:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _tab_key():
    key = os.environ.get("TABIT_AGENT") or ""
    pane = os.environ.get("TMUX_PANE") or ""
    if not pane:
        return key
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#S"],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def main(argv):
    try:
        if len(argv) != 2:
            return 0
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return 0
        key = _tab_key()
        if not key:
            return 0
        apply(argv[1], key, payload)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
