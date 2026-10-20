#!/usr/bin/env python3
"""Read workspace and redaction settings for the kit."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

DRIVE_NAMES = ("My Drive", "我的云端硬盘")


def load_config(config_path: str | Path | None = None) -> dict:
    explicit = config_path or os.environ.get("VOYAGER_WEB_PRO_CONFIG")
    base = Path(os.environ["VOYAGER_HOME"]) if os.environ.get("VOYAGER_HOME") else Path.home() / ".voyager"
    path = Path(explicit).expanduser() if explicit else base / "web-pro-kit.json"
    config = {"workspace": None, "redact": []}
    if path.exists() or explicit:
        config.update(json.loads(path.read_text(encoding="utf-8-sig")))  # Notepad may add a BOM.
    workspace = os.environ.get("VOYAGER_WEB_PRO_WORKSPACE", config["workspace"])
    if workspace:
        resolved = Path(workspace).expanduser()
        if not resolved.is_absolute():
            base = Path.cwd() if "VOYAGER_WEB_PRO_WORKSPACE" in os.environ else path.parent
            resolved = base / resolved
        config["workspace"] = str(resolved)
    return config


def _drive_roots() -> list[Path]:
    candidates = []
    home = Path.home()
    if sys.platform == "win32":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            candidates.extend(Path(f"{letter}:/") / name for name in DRIVE_NAMES)
        folders = {home / "Google Drive", home / "GoogleDrive", *home.glob("Google Drive*"), *home.glob("GoogleDrive*")}
        for folder in sorted(folders):
            if folder.is_dir():
                nested = [folder / name for name in DRIVE_NAMES if (folder / name).is_dir()]
                candidates.extend(nested or [folder])
    elif sys.platform == "darwin":
        cloud = home / "Library" / "CloudStorage"
        for folder in sorted(cloud.glob("GoogleDrive-*")):
            candidates.extend(folder / name for name in DRIVE_NAMES)
    roots = {str(path.resolve()): path.resolve() for path in candidates if path.is_dir()}
    return sorted(roots.values(), key=str)


def workspace_root(config: dict | None = None) -> Path:
    config = load_config() if config is None else config
    explicit = os.environ.get("VOYAGER_WEB_PRO_WORKSPACE", config.get("workspace"))
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise ValueError(f"Workspace directory does not exist: {path}; create it or correct workspace")
        return path.resolve()
    roots = _drive_roots()
    if len(roots) > 1:
        raise ValueError("Multiple Google Drive roots found; set workspace explicitly: " + ", ".join(map(str, roots)))
    if not roots:
        raise ValueError("No Google Drive desktop root found; set workspace to an existing directory or rclone mount, or use --out")
    path = roots[0] / "Voyager-workspace"
    if not path.is_dir():
        raise ValueError(f"Create workspace directory first: {path}; or set workspace explicitly")
    return path.resolve()


def selftest() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        path = root / "web-pro-kit.json"
        with patch.dict(os.environ, {"VOYAGER_HOME": str(root)}, clear=True):
            assert load_config() == {"workspace": None, "redact": []}
            path.write_text(json.dumps({"workspace": "workspace", "redact": ["sample"]}), encoding="utf-8")
            assert load_config(path)["redact"] == ["sample"]
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
            assert load_config(path)["redact"] == ["sample"]
            assert workspace_root(load_config()) == workspace.resolve()
            with patch.dict(os.environ, {"VOYAGER_WEB_PRO_WORKSPACE": str(root)}):
                assert workspace_root(load_config()) == root.resolve()
            with patch.dict(os.environ, {"VOYAGER_WEB_PRO_CONFIG": str(path)}):
                assert load_config()["workspace"] == str(workspace)
            with patch(__name__ + "._drive_roots", return_value=[root]):
                (root / "Voyager-workspace").mkdir()
                assert workspace_root({}) == (root / "Voyager-workspace").resolve()
    print("selftest PASS: 7 checks")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        if args.selftest:
            raise SystemExit(selftest())
        print(workspace_root(load_config(args.config)))
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")
