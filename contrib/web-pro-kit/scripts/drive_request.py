#!/usr/bin/env python3
"""Append a numbered request to a task's synced REQUEST.md."""
from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import re
import sys
import tempfile
from kit_config import load_config, workspace_root

HEAD_RE = re.compile(r"^Version: (\d+)", re.M)


def request_path(workspace: Path, tag: str) -> Path:
    if not tag or tag in {".", ".."} or any(ch in tag for ch in "/\\:"):
        raise ValueError("Use a single task folder name")
    return workspace / tag / "REQUEST.md"


def append(request: Path, body: str) -> int:
    old = request.read_text(encoding="utf-8") if request.exists() else ""
    match = HEAD_RE.search(old)
    version = int(match.group(1)) + 1 if match else 1
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    entries = old.split("\n## ", 1)[1] if "\n## " in old else ""
    head = f"Version: {version}\nUpdated: {now}\n\nLater requests take precedence.\n"
    new_entry = f"## Request {version} ({now})\n\n{body.strip()}\n"
    text = head + ("\n## " + entries.rstrip() + "\n" if entries else "") + "\n" + new_entry
    request.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=request.parent, delete=False) as pending:
        pending.write(text.encode("utf-8"))
    os.replace(pending.name, request)
    return version


def selftest() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        request = request_path(workspace, "sample-task")
        assert append(request, "First") == 1
        assert append(request, "Second") == 2
        assert append(request, "Third") == 3
        text = request.read_text(encoding="utf-8")
        assert text.startswith("Version: 3")
        assert text.index("First") < text.index("Second") < text.index("Third")
        assert text.count("## Request ") == 3
        for tag in ("../other-task", "/other-task"):
            try:
                request_path(workspace, tag)
            except ValueError:
                pass
            else:
                raise AssertionError("Expected a single task folder name")
    print("selftest PASS: 8 checks")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.tag:
        parser.error("provide a task ID")
    try:
        request = request_path(workspace_root(load_config(args.config)), args.tag)
        if args.show:
            print(request.read_text(encoding="utf-8") if request.exists() else "(No requests yet)")
            return 0
        body = args.file.read_text(encoding="utf-8") if args.file else (args.text or "")
        if not body.strip():
            parser.error("provide request text or --file")
        version = append(request, body)
        print(f"Wrote {request} (version {version}); read at the next checkpoint after sync.")
        return 0
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
