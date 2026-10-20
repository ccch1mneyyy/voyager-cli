#!/usr/bin/env python3
"""Wait for a synced DONE.txt or quiet new outputs.

Exit 0 reports the marker; exit 2 asks for a delivery review after quiet output.
The quiet timer tracks outputs written after startup and ignores control files.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


CONTROL_FILES = {"REQUEST.md", "test.txt"}


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    """Track output writes recursively."""
    files = {}

    def on_error(error):
        raise error

    if not directory.exists():
        return files
    for root, _dirs, names in os.walk(directory, onerror=on_error):
        for name in names:
            path = Path(root, name)
            if name in CONTROL_FILES:
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            files[str(path.relative_to(directory))] = (stat.st_mtime_ns, stat.st_size)
    return files


def _listing(directory: Path) -> None:
    for name, (_, size) in sorted(_snapshot(directory).items()):
        print(f"  {size:10d}  {name}", flush=True)


def wait(path: Path, every: float, quiet_min: float = 10.0, settle: float = 2.0) -> int:
    started = time.monotonic()
    directory = path.parent
    previous = _snapshot(directory)
    last_change = None
    print(
        f"[drive_wait] Waiting for {path}; poll {every:g}s; "
        f"quiet fallback {quiet_min:g} minutes (0 disables it).",
        flush=True,
    )
    while not path.is_file():
        time.sleep(every)
        if path.is_file():
            break
        if not quiet_min:
            continue
        try:
            current = _snapshot(directory)
        except OSError:
            # Restart the quiet timer after a read error.
            last_change = None
            continue
        now = time.monotonic()
        wrote_output = any(previous.get(name) != stamp for name, stamp in current.items())
        if wrote_output or (last_change is not None and current != previous):
            last_change = now
        previous = current
        if last_change is not None and now - last_change >= quiet_min * 60:
            print(
                "[drive_wait] No DONE marker: new outputs have gone quiet. "
                "Check the conversation, ZIP and NOTES.md before accepting delivery.",
                flush=True,
            )
            _listing(directory)
            return 2
    time.sleep(settle)
    body = path.read_text(encoding="utf-8-sig").strip()
    print(f"[drive_wait] Marker arrived after {(time.monotonic() - started) / 60:.1f} minutes.")
    print(f"---- {path.name} ----\n{body}\n------------------", flush=True)
    _listing(directory)
    return 0


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def selftest() -> int:
    class WaitTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory()
            self.addCleanup(self.temp.cleanup)
            self.root = Path(self.temp.name)
            self.marker = self.root / "DONE.txt"

        def later(self, delay, action):
            timer = threading.Timer(delay, action)
            timer.start()
            self.addCleanup(timer.join)

        def done(self):
            self.marker.write_text("finished 1 / unfinished 0 / result.zip\n", encoding="utf-8")

        def test_existing_marker(self):
            self.done()
            self.assertEqual(wait(self.marker, 0.01, 0, settle=0), 0)

        def test_delayed_marker(self):
            self.later(0.03, self.done)
            self.assertEqual(wait(self.marker, 0.01, 0, settle=0), 0)

        def test_quiet_output(self):
            self.later(0.03, lambda: (self.root / "result.zip").write_bytes(b"example"))
            self.assertEqual(wait(self.marker, 0.01, 0.001, settle=0), 2)

        def test_existing_outputs_do_not_complete(self):
            (self.root / "old.zip").write_bytes(b"example")
            self.later(0.15, self.done)
            self.assertEqual(wait(self.marker, 0.01, 0.001, settle=0), 0)

        def test_control_files_do_not_complete(self):
            self.later(0.01, lambda: (self.root / "test.txt").write_bytes(b""))
            self.later(0.02, lambda: (self.root / "REQUEST.md").write_text("Version: 1\n"))
            self.later(0.15, self.done)
            self.assertEqual(wait(self.marker, 0.01, 0.001, settle=0), 0)

        def test_snapshot_detects_nested_size_changes(self):
            (self.root / "nested").mkdir()
            path = self.root / "nested" / "result.txt"
            path.write_text("one")
            before = _snapshot(self.root)
            path.write_text("two results")
            self.assertNotEqual(before, _snapshot(self.root))
            self.assertIn(str(Path("nested", "result.txt")), before)

        def test_invalid_intervals(self):
            for value in ("-1", "nan", "inf"):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _nonnegative(value)
                with self.assertRaises(argparse.ArgumentTypeError):
                    _positive(value)
            with self.assertRaises(argparse.ArgumentTypeError):
                _positive("0")
            self.assertEqual(_nonnegative("0"), 0)

    result = unittest.TextTestRunner(verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WaitTests)
    )
    print(f"selftest {'PASS' if result.wasSuccessful() else 'FAIL'} ({result.testsRun} tests)")
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("marker", nargs="?", type=Path, help="local synced path to DONE.txt")
    parser.add_argument("--every", type=_positive, default=20.0, help="poll interval in seconds")
    parser.add_argument("--quiet", type=_nonnegative, default=10.0, help="quiet minutes; 0 disables")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.marker is None:
        parser.error("provide the local path to the task's DONE.txt")
    try:
        return wait(args.marker.expanduser(), args.every, args.quiet)
    except OSError as error:
        print(f"[drive_wait] Cannot read task files: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
