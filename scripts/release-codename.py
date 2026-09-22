#!/usr/bin/env python3
"""Release codename helpers for tabit (history + title — no public future pool).

Codenames are Taiwan-origin cuisine English names. There is no candidate menu
in the repo; RD picks a fresh name when cutting a release. This file only
tracks names already shipped (to avoid reuse) and formats the release title
from APP_VERSION / APP_CODENAME in tabit.py.

Rules (also in README):
  - Must be Taiwan-origin cuisine (not limited to street snacks)
  - Major/minor (1st/2nd version digits): more internationally known names
  - Patch (3rd digit): more obscure names
  - Public label is English only; optional Chinese stays in private notes

Usage:
  python3 scripts/release-codename.py list
  python3 scripts/release-codename.py title              # from APP_* in tabit.py
  python3 scripts/release-codename.py title v1.7.12      # look up shipped history
  python3 scripts/release-codename.py record v1.7.12 --name "Braised Pork Rice"
  python3 scripts/release-codename.py check              # APP_* vs history

When cutting a release:
  1. Pick a fresh English codename per the rules (not already in `list`).
  2. Set APP_VERSION and APP_CODENAME in tabit.py.
  3. python3 scripts/release-codename.py record <tag> --name "<English>"
  4. Create the GitHub release with that title, e.g.:
       TITLE=$(python3 scripts/release-codename.py title)
       gh release create v1.7.12 -t "$TITLE" -F .release/v1.7.12-notes.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODENAMES_FILE = ROOT / "release-codenames.json"
TABIT_PY = ROOT / "tabit.py"
SEP = " · "


def load():
    data = json.loads(CODENAMES_FILE.read_text(encoding="utf-8"))
    items = data.get("shipped")
    if items is None:
        # Migrate legacy "codenames" + used_by shape if someone still has it.
        legacy = data.get("codenames") or []
        items = [
            {"version": c["used_by"], "english": c["english"]}
            for c in legacy
            if c.get("used_by")
        ]
        data = {"_comment": data.get("_comment", ""), "shipped": items}
    if not isinstance(items, list):
        raise SystemExit(f"bad {CODENAMES_FILE}: expected shipped list")
    return data, items


def save(data):
    CODENAMES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def format_label(version: str, english: str) -> str:
    version = (version or "").strip()
    english = (english or "").strip()
    if version and english:
        return f"{version}{SEP}{english}"
    return version or english


def find_by_version(items, version: str):
    for c in items:
        if c.get("version") == version:
            return c
    return None


def find_by_english(items, name: str):
    want = name.strip().lower()
    for c in items:
        if (c.get("english") or "").strip().lower() == want:
            return c
    return None


def read_app_constants():
    text = TABIT_PY.read_text(encoding="utf-8")
    ver = re.search(r'^APP_VERSION\s*=\s*"([^"]*)"', text, re.M)
    code = re.search(r'^APP_CODENAME\s*=\s*"([^"]*)"', text, re.M)
    return (ver.group(1) if ver else ""), (code.group(1) if code else "")


def cmd_list(_args):
    _, items = load()
    if not items:
        print("(no shipped codenames yet)")
        return
    for c in items:
        print(f"{c.get('version', '?'):10}  {c.get('english', '')}")


def cmd_title(args):
    _, items = load()
    version = args.version
    if not version:
        ver, code = read_app_constants()
        if not ver:
            raise SystemExit("APP_VERSION not found in tabit.py")
        print(format_label(ver, code))
        return
    hit = find_by_version(items, version)
    if hit:
        print(format_label(version, hit["english"]))
        return
    ver, code = read_app_constants()
    if ver == version and code:
        print(format_label(version, code))
        print(
            f"# not in history yet; using APP_CODENAME from tabit.py",
            file=sys.stderr,
        )
        return
    raise SystemExit(
        f"no shipped codename for {version}; "
        "set APP_CODENAME in tabit.py or: "
        f'python3 scripts/release-codename.py record {version} --name "…"'
    )


def cmd_record(args):
    data, items = load()
    version = args.version.strip()
    name = (args.name or "").strip()
    if not version.startswith("v"):
        raise SystemExit("version must look like v1.7.12")
    if not name:
        raise SystemExit("--name is required (pick a fresh English cuisine name)")

    existing_ver = find_by_version(items, version)
    existing_name = find_by_english(items, name)

    if existing_name and existing_name.get("version") != version and not args.force:
        raise SystemExit(
            f"{existing_name['english']} already shipped as "
            f"{existing_name['version']}; pick another name (or --force)"
        )
    if existing_ver and not args.force:
        if (existing_ver.get("english") or "").strip().lower() == name.lower():
            print(format_label(version, existing_ver["english"]))
            print("# already recorded", file=sys.stderr)
            return
        raise SystemExit(
            f"{version} already recorded as {existing_ver['english']}; "
            "use --force to replace"
        )

    if args.force:
        # Replace this version's entry, nothing else. Dropping every entry
        # with the same name deleted the release a hotfix was fixing.
        items[:] = [c for c in items if c.get("version") != version]

    items.append({"version": version, "english": name})
    # Keep chronological-ish order by version string (good enough for semver tags).
    items.sort(key=lambda c: c.get("version") or "")
    data["shipped"] = items
    save(data)
    label = format_label(version, name)
    print(label)
    print(
        f'# recorded; ensure APP_CODENAME = "{name}" in tabit.py',
        file=sys.stderr,
    )


def cmd_check(_args):
    """Verify APP_VERSION / APP_CODENAME match a shipped history entry."""
    _, items = load()
    ver, code = read_app_constants()
    if not ver or not code:
        raise SystemExit("APP_VERSION / APP_CODENAME missing in tabit.py")
    hit = find_by_version(items, ver)
    if not hit:
        raise SystemExit(
            f"{ver} not in release-codenames.json history; "
            f'run: python3 scripts/release-codename.py record {ver} --name "{code}"'
        )
    if (hit.get("english") or "").strip() != code.strip():
        raise SystemExit(
            f"mismatch: tabit.py has {ver} · {code!r}, "
            f"history has {ver} · {hit.get('english')!r}"
        )
    print(format_label(ver, code))
    print("# OK", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show shipped codename history (no future pool)")
    sub.add_parser("check", help="verify APP_* matches shipped history")

    p_title = sub.add_parser("title", help="print 'vX.Y.Z · Name' label")
    p_title.add_argument(
        "version", nargs="?", help="tag (default: APP_VERSION + APP_CODENAME)")

    p_record = sub.add_parser(
        "record", help="append a shipped name to history (required --name)")
    p_record.add_argument("version", help="release tag, e.g. v1.7.12")
    p_record.add_argument(
        "--name", required=True, help="English cuisine codename just shipped")
    p_record.add_argument("--force", action="store_true", help="replace existing")
    # Alias kept so older docs/muscle-memory still work.
    p_assign = sub.add_parser(
        "assign", help="alias for record (requires --name)")
    p_assign.add_argument("version", help="release tag, e.g. v1.7.12")
    p_assign.add_argument(
        "--name", required=True, help="English cuisine codename just shipped")
    p_assign.add_argument("--force", action="store_true", help="replace existing")

    args = ap.parse_args()
    {
        "list": cmd_list,
        "title": cmd_title,
        "record": cmd_record,
        "assign": cmd_record,
        "check": cmd_check,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
