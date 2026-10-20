#!/usr/bin/env python3
"""Shared credential shapes used by the mirror redactor."""
import re
import sys

BLOCK_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]{2,}")),
    ("win-home", re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!Public\b|Default\b|All Users\b)[A-Za-z0-9._-]+")),
    ("nix-home", re.compile(r"/(?:home|Users)/(?!shared\b)[A-Za-z0-9._-]+")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}")),
]


def selftest() -> int:
    cases = {
        "email": "tester@" + "example.invalid",
        "win-home": "C:/Users/" + "SampleUser/file",
        "nix-home": "/home/" + "sample/file",
        "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "openai-key": "sk-" + "a" * 20,
        "github-token": "ghp_" + "a" * 24,
        "aws-key": "AKIA" + "A" * 16,
        "slack-token": "xoxb-" + "a" * 14,
        "google-key": "AIza" + "a" * 32,
        "jwt": "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 10,
    }
    for name, pattern in BLOCK_PATTERNS:
        assert pattern.search(cases[name]), name
        assert not pattern.search("plain neutral text"), name
    print(f"selftest PASS: {len(cases) * 2} checks")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.argv[1:] == ["--selftest"]:
        raise SystemExit(selftest())
    raise SystemExit("Use --selftest; import BLOCK_PATTERNS from this module.")
