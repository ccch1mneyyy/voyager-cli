#!/usr/bin/env python3
"""MIRROR.json layers, the published manifest, and the local report of files kept back."""
from __future__ import annotations
import datetime as _dt
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from redaction import RULES_VERSION, Redactor
from safe_files import sha256


def read_mirror(d: Path) -> dict:
    try:
        return json.loads((d / "MIRROR.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def new_layer(files: list[Path], commit: str, count: int, **extra) -> dict:
    return {'files': [f.name for f in files], 'bytes': [f.stat().st_size for f in files], 'sha256': [sha256(f) for f in files], 'file_count': count, 'commit': commit, **extra}


def layer_ok(dest: Path, layer: dict | None) -> bool:
    """Published volumes match the manifest byte for byte; a same-size corruption fails here."""
    if not layer or len(layer.get("sha256") or []) != len(layer["files"]):
        return False
    return all((dest / name).is_file() and (dest / name).stat().st_size == size and sha256(dest / name) == digest
               for name, size, digest in zip(layer["files"], layer["bytes"], layer["sha256"]))


def policy_fingerprint(red: Redactor) -> str:
    rules = [(word, pattern.pattern, replacement) for word, pattern, replacement in red.words]
    return hashlib.sha256(json.dumps([RULES_VERSION, rules, red.bin_needles], sort_keys=True).encode()).hexdigest()


def shown(path: str, reason: str) -> str:
    # A path that matched a private term must not reach the synced manifest.
    return f"<path withheld: {reason[6:]}>" if reason.startswith("path: ") else path


def print_summary(result: dict) -> None:
    blocked = result["blocked"]
    if blocked:
        print(f"{len(blocked)} files not uploaded (listed in MIRROR.json excluded_files):")
        for path, reason in blocked:
            note = "; path withheld from MIRROR.json" if reason.startswith("path: ") else ""
            print(f"  {path}  ({reason}{note})")
    if result["non_utf8"]:
        print(f"warning: {len(result['non_utf8'])} non-UTF-8 text files uploaded unchanged; no private term or "
              "credential pattern was found, but only UTF-8 text is redacted. Convert them to UTF-8 to be sure:")
        for path in result["non_utf8"]:
            print(f"  {path}")
    if result["collisions"]:
        print("warning: names that differ only by case or Unicode normalization; they overwrite each other "
              "when extracted on Windows or macOS:")
        for first, second in result["collisions"]:
            print(f"  {first}  <->  {second}")




def build_manifest(name: str, head: str, result: dict, policy: str, layers: dict, dirty: int, prev: dict, need_text: bool) -> dict:
    kinds = {}
    for _path, _line, kind in result["redactions"]:
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "tool": "voyager-web-pro-kit/drive-mirror/1", "project": name,
        "commit": head, "commit_date": result["commit_date"], "subject": result["subject"],
        "pushed_at": _dt.datetime.now().astimezone().isoformat(timespec="microseconds"),
        "worktree": {"uncommitted_changes_excluded": dirty}, "redaction_policy": policy, "layers": layers,
        "redactions": {"count": len(result["redactions"]), "by_kind": kinds,
            "where": "text archive: _mirror/redactions.tsv"} if need_text else prev.get("redactions"),
        "excluded_binary_files": result["excluded"],
        "excluded_files": [{"path": shown(p, r), "reason": r} for p, r in result["blocked"]],
        "how_to": ["Extract text into a new empty directory, then optionally extract binary volumes there.",
            "Check the text archive's _mirror/STAMP.json commit against MIRROR.json and the requested commit.",
            "Wait for Drive sync, then verify the files through the connected app.",
            "Binary same_as_text_commit describes content equivalence; its commit may be older.",
            "Files in excluded_files were kept back on purpose; ask for them rather than guessing.",
            "Inspect the first export before sharing."],
    }


def write_report(path: Path, name: str, head: str, dest: Path, result: dict) -> None:
    """Local only: full paths, including ones withheld from the synced manifest."""
    path.write_text(json.dumps({"project": name, "commit": head, "destination": str(dest),
        "not_uploaded": [{"path": p, "reason": r} for p, r in result["blocked"]],
        "non_utf8_uploaded_unchanged": result["non_utf8"], "name_collisions": result["collisions"]},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Local report: {path}")


def selftest() -> int:
    checks = 0

    def check(value):
        nonlocal checks
        assert value
        checks += 1
    result = {"redactions": [("a.py", 1, "email")], "commit_date": "", "subject": "", "excluded": [], "non_utf8": [],
              "collisions": [], "blocked": [("PrivateName/a.txt", "path: personal"), ("k.pem", "unclosed private key")]}
    manifest = build_manifest("sample", "0" * 40, result, "", {}, 0, {}, True)
    check(manifest["excluded_files"] == [{"path": "<path withheld: personal>", "reason": "path: personal"},
                                         {"path": "k.pem", "reason": "unclosed private key"}])
    check("PrivateName" not in json.dumps(manifest) and manifest["redactions"]["by_kind"] == {"email": 1})
    with tempfile.TemporaryDirectory(prefix="mirror-manifest-test-") as td:
        root = Path(td)
        (root / "a.zip").write_bytes(b"archive bytes")
        layer = new_layer([root / "a.zip"], "0" * 40, 1)
        check(layer_ok(root, layer))
        (root / "a.zip").write_bytes(b"archive bytez")  # Same size, different content.
        check(not layer_ok(root, layer) and not layer_ok(root, dict(layer, sha256=[])))
        write_report(root / "report.json", "sample", "0" * 40, root, result)
        check(json.loads((root / "report.json").read_text(encoding="utf-8"))["not_uploaded"][0]["path"] == "PrivateName/a.txt")
    check(policy_fingerprint(Redactor([], ["a"])) != policy_fingerprint(Redactor([], ["b"])))
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
