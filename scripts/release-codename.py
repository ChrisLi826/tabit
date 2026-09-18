#!/usr/bin/env python3
"""Pick / assign Taiwanese-snack release codenames for tabit.

Usage:
  python3 scripts/release-codename.py list
  python3 scripts/release-codename.py next
  python3 scripts/release-codename.py title              # from APP_* in tabit.py
  python3 scripts/release-codename.py title v1.7.12      # look up used_by / next
  python3 scripts/release-codename.py assign v1.7.12     # mark next unused as used
  python3 scripts/release-codename.py assign v1.7.12 --name "Stinky Tofu"

When cutting a release:
  1. python3 scripts/release-codename.py next
  2. Set APP_VERSION and APP_CODENAME in tabit.py to match.
  3. python3 scripts/release-codename.py assign <tag>
  4. Create the GitHub release with that title, e.g.:
       TITLE=$(python3 scripts/release-codename.py title v1.7.12)
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
    items = data.get("codenames") or []
    if not isinstance(items, list):
        raise SystemExit(f"bad {CODENAMES_FILE}: expected codenames list")
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


def unused(items):
    return [c for c in items if not c.get("used_by")]


def find_by_version(items, version: str):
    for c in items:
        if c.get("used_by") == version:
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
    for c in items:
        en = c.get("english", "")
        zh = c.get("chinese", "")
        used = c.get("used_by") or "—"
        mark = "used" if c.get("used_by") else "free"
        print(f"[{mark:4}] {en} / {zh}  →  {used}")


def cmd_next(_args):
    _, items = load()
    pool = unused(items)
    if not pool:
        raise SystemExit("no unused codenames left — add more snacks to release-codenames.json")
    c = pool[0]
    print(c["english"])
    print(f"# Chinese: {c.get('chinese', '')}", file=sys.stderr)


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
    pool = unused(items)
    if not pool:
        raise SystemExit(f"no codename for {version} and none unused")
    print(format_label(version, pool[0]["english"]))
    print(f"# not assigned yet; next unused would be: {pool[0]['english']}", file=sys.stderr)


def cmd_assign(args):
    data, items = load()
    version = args.version.strip()
    if not version.startswith("v"):
        raise SystemExit("version must look like v1.7.12")
    existing = find_by_version(items, version)
    if existing and not args.force:
        raise SystemExit(
            f"{version} already assigned to {existing['english']}; "
            "use --force to reassign"
        )
    if args.name:
        target = find_by_english(items, args.name)
        if not target:
            raise SystemExit(f"unknown English name: {args.name}")
        if target.get("used_by") and target["used_by"] != version and not args.force:
            raise SystemExit(
                f"{target['english']} already used by {target['used_by']}; "
                "use --force"
            )
    else:
        pool = unused(items)
        if existing and args.force:
            target = existing
        elif not pool:
            raise SystemExit("no unused codenames left")
        else:
            target = pool[0]

    if args.force:
        for c in items:
            if c.get("used_by") == version and c is not target:
                c["used_by"] = None

    target["used_by"] = version
    save(data)
    label = format_label(version, target["english"])
    print(label)
    print(
        f"# marked used_by={version}; set APP_CODENAME = \"{target['english']}\" in tabit.py",
        file=sys.stderr,
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show used vs unused")
    sub.add_parser("next", help="print next unused English name")

    p_title = sub.add_parser("title", help="print 'vX.Y.Z · Snack' label")
    p_title.add_argument(
        "version", nargs="?", help="tag (default: APP_VERSION + APP_CODENAME)")

    p_assign = sub.add_parser("assign", help="assign next unused snack to a version")
    p_assign.add_argument("version", help="release tag, e.g. v1.7.12")
    p_assign.add_argument("--name", help="English snack name (default: next unused)")
    p_assign.add_argument("--force", action="store_true", help="allow reassignment")

    args = ap.parse_args()
    {"list": cmd_list, "next": cmd_next, "title": cmd_title, "assign": cmd_assign}[args.cmd](args)


if __name__ == "__main__":
    main()
