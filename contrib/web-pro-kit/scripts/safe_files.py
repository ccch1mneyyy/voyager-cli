#!/usr/bin/env python3
"""Portable names and replace-in-place publishing for synced folders."""
from __future__ import annotations
import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
REPLACE_TRIES = 8
REPLACE_DELAY = 0.25  # Seconds between retries while Drive or an editor holds the file.


def safe_segment(value: str | None, label: str = "name") -> str:
    """Return value when it is one folder/file name that works on Windows, macOS and Linux."""
    text = value or ""
    if not text or text in {".", ".."}:
        raise ValueError(f"{label} must be a single folder name, not {text!r}")
    if UNSAFE_CHARS.search(text):
        raise ValueError(f"{label} {text!r} must not contain path separators, a drive letter, or any of <>:\"|?*")
    if text[-1] in ". " or text[0] == " ":
        raise ValueError(f"{label} {text!r} must not start with a space or end with a dot or space")
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        raise ValueError(f"{label} {text!r} is a reserved Windows device name")
    return text


def inside(parent: Path, child: Path, label: str = "path") -> Path:
    """Return child when it stays inside parent after resolving links and '..'."""
    base, target = Path(parent).resolve(), Path(child).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"{label} {child} leaves {parent}")
    return Path(child)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def replace_file(source: Path, target: Path) -> None:
    """os.replace, retried briefly: Windows refuses while another process has the target open."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_DELAY)


def _publish(target: Path, prefix: str, fill, expected: str) -> None:
    target = Path(target)
    fd, pending = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            fill(handle)
            handle.flush()
            os.fsync(handle.fileno())
        if sha256(Path(pending)) != expected:
            raise ValueError(f"Read-back checksum mismatch before publishing {target.name}")
        replace_file(Path(pending), target)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def write_atomic(target: Path, data: bytes, prefix: str = ".mirror-") -> None:
    """Write beside target, verify, then replace it, so readers never see half a file."""
    _publish(target, prefix, lambda handle: handle.write(data), hashlib.sha256(data).hexdigest())


def copy_atomic(source: Path, target: Path, prefix: str = ".mirror-") -> None:
    def fill(handle):
        with open(source, "rb") as src:
            for block in iter(lambda: src.read(1 << 20), b""):
                handle.write(block)
    _publish(target, prefix, fill, sha256(Path(source)))


def selftest() -> int:
    global REPLACE_DELAY
    checks = 0

    def check(value):
        nonlocal checks
        assert value
        checks += 1
    for good in ("sample", "kit-1.2", "镜像", "my project"):
        check(safe_segment(good) == good)
    for bad in ("", ".", "..", "a/b", "a\\b", "/abs", "C:", "C:\\x", "\\\\host\\share", "CON", "nul.txt",
                "com1", "LPT9", "trail.", "trail ", " lead", "a?b", "tab\tname"):
        try:
            safe_segment(bad)
        except ValueError:
            checks += 1
        else:
            raise AssertionError(f"accepted {bad!r}")
    saved_delay, real_replace = REPLACE_DELAY, os.replace
    REPLACE_DELAY = 0
    try:
        with tempfile.TemporaryDirectory(prefix="safe-files-test-") as td:
            root = Path(td)
            check(inside(root, root / "a" / "b") == root / "a" / "b")
            try:
                inside(root / "a", root / "a" / ".." / "b")
            except ValueError:
                checks += 1
            else:
                raise AssertionError("escaped parent accepted")
            target = root / "REQUEST.md"
            target.write_bytes(b"old")
            failures = {"left": 2}

            def flaky(src, dst):
                if failures["left"]:
                    failures["left"] -= 1
                    raise PermissionError(32, "in use")
                real_replace(src, dst)
            with patch("os.replace", flaky):
                write_atomic(target, b"new", prefix=".request-")
            check(target.read_bytes() == b"new")
            check(sorted(p.name for p in root.iterdir()) == ["REQUEST.md"])

            def locked(src, dst):
                raise PermissionError(32, "in use")
            with patch("os.replace", locked):
                try:
                    write_atomic(target, b"lost", prefix=".request-")
                except PermissionError:
                    checks += 1
                else:
                    raise AssertionError("locked target reported success")
            check(target.read_bytes() == b"new")
            check(sorted(p.name for p in root.iterdir()) == ["REQUEST.md"])
            source = root / "source.bin"
            source.write_bytes(b"\0archive" * 1000)
            copy_atomic(source, root / "copy.bin")
            check((root / "copy.bin").read_bytes() == source.read_bytes())
    finally:
        REPLACE_DELAY = saved_delay
    print(f"selftest PASS: {checks} checks")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    print(__doc__)
