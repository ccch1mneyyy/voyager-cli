#!/usr/bin/env python3
"""Build the text and binary ZIPs for one commit; used by drive_mirror.py."""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import zipfile
from pathlib import Path
from code_index import EXT_KIND, build as index_build, verify as index_verify
from redaction import Redactor
from safe_files import inside

CHUNK = 200_000_000  # Local binary ZIP volume limit in bytes.
TEXT_MAX = 250 * 2 ** 20  # Maximum text ZIP size.
STORE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ogg", ".mp3", ".mp4", ".webm", ".zip", ".gz", ".7z", ".woff2", ".ktx2", ".basis"}
RESERVED_TOP = {"index", "_mirror"}  # The text ZIP's own INDEX/ and _mirror/ folders.


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode('utf-8', 'replace')


def repo_root(start: Path) -> Path:
    return Path(git(start, 'rev-parse', '--show-toplevel').strip())


def _zinfo(name: str, mode: int, dt: tuple) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=dt)
    zi.create_system = 3
    zi.external_attr = (mode & 511 | 32768) << 16
    zi.compress_type = zipfile.ZIP_STORED if Path(name).suffix.lower() in STORE_EXT else zipfile.ZIP_DEFLATED
    return zi


class ChunkWriter:
    def __init__(self, tmp: Path, stem: str):
        self.tmp, self.stem, self.parts, self.z, self.size = tmp, stem, [], None, 0

    def add(self, name: str, data: bytes, mode: int, dt: tuple) -> None:
        if self.z is None or (self.size and self.size + len(data) > CHUNK):
            self.close()
            p = self.tmp / f"{self.stem}.part{len(self.parts) + 1}"
            self.parts.append(p)
            self.z, self.size = zipfile.ZipFile(p, "w"), 0
        self.z.writestr(_zinfo(name, mode, dt), data)
        self.size += len(data)

    def close(self) -> None:
        if self.z is not None:
            self.z.close()
            self.z = None

    def finish(self) -> list[Path]:
        self.close()
        n = len(self.parts)
        out = []
        for i, p in enumerate(self.parts, 1):
            q = p.with_name(f"{self.stem}.zip" if n == 1 else f"{self.stem}-{i}of{n}.zip")
            p.rename(q)
            out.append(q)
        return out


def name_collisions(paths: list[str]) -> list[tuple[str, str]]:
    """Pairs that Windows or macOS would extract to one file: same name up to case or NFC/NFD."""
    seen: dict[str, str] = {}
    pairs = []
    for p in sorted(paths):
        key = unicodedata.normalize('NFC', p).casefold()
        if key in seen:
            pairs.append((seen[key], p))
        else:
            seen[key] = p
    return sorted(pairs)


def reserved_paths(paths: list[str]) -> list[str]:
    return sorted(p for p in paths if p.split('/', 1)[0].rstrip('/').casefold() in RESERVED_TOP)


def build(repo: Path, name: str, commit: str, tmp: Path, red: Redactor, want_text: bool, want_bin: bool) -> dict:
    info = git(repo, 'show', '-s', '--format=%H%n%ct%n%cI%n%s', commit).split('\n', 3)
    full, ct, cdate, subject = (info[0], int(info[1]), info[2], info[3].strip())
    subject = red.text(subject.encode('utf-8'))[0].decode('utf-8')
    dt = time.gmtime(max(ct, 315532800))[:6]
    proc = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", full], stdout=subprocess.PIPE)
    texts: dict[str, tuple[bytes, int]] = {}
    bins: list[tuple[str, int, str]] = []
    redlog: list[tuple[str, int, str]] = []
    excluded: list[str] = []
    blocked: list[tuple[str, str]] = []
    non_utf8: list[str] = []
    members: list[str] = []
    kept: list[str] = []
    fp = hashlib.sha256()
    bw = ChunkWriter(tmp, f'{name}-bin') if want_bin else None
    with tarfile.open(fileobj=proc.stdout, mode='r|') as tf:
        for m in tf:
            members.append(m.name)
            if not m.isfile():
                continue  # Symbolic links and submodules are not exported.
            data = tf.extractfile(m).read()
            v = red.scan(m.name, data)
            if v.kind == 'blocked':
                blocked.append((m.name, v.reason))
                if v.reason.startswith('binary'):
                    excluded.append(m.name)
                continue
            kept.append(m.name)
            if v.kind == 'text':
                if v.note == 'non-utf8':
                    non_utf8.append(m.name)
                redlog += [(m.name, ln, k) for ln, k in v.hits]
                if want_text:
                    texts[m.name] = (v.data, m.mode)
                continue
            digest = hashlib.sha1(data).hexdigest()
            fp.update(f'{m.name}\x00{digest}\n'.encode('utf-8', 'surrogateescape'))
            bins.append((m.name, len(data), digest[:12]))
            if bw:
                bw.add(m.name, data, m.mode, dt)
    if proc.wait() != 0:
        raise ValueError("git archive failed")
    clash = reserved_paths(members)
    if clash:
        raise ValueError("The repository has paths in the mirror's own INDEX/ or _mirror/ folders: "
                         + ", ".join(clash[:10]) + "; rename them or mark them export-ignore")
    out = {'commit': full, 'commit_date': cdate, 'subject': subject, 'bin_fingerprint': fp.hexdigest(), 'bin_count': len(bins),
           'excluded': excluded, 'blocked': blocked, 'non_utf8': non_utf8, 'collisions': name_collisions(kept),
           'text_zip': None, 'bin_zips': bw.finish() if bw else [], 'text_count': len(texts), 'redactions': redlog}
    if not want_text:
        return out
    stamp = {'project': name, 'layer': 'text', 'commit': full, 'commit_date': cdate, 'subject': subject, 'note': 'Committed text only; binary inventory is in _mirror/binary-files.tsv.'}
    meta = {'_mirror/STAMP.json': json.dumps(stamp, ensure_ascii=False, indent=1).encode('utf-8'), '_mirror/binary-files.tsv': ('path\tbytes\tsha1_12\n' + ''.join((f'{p}\t{n}\t{h}\n' for p, n, h in bins))).encode('utf-8', 'surrogateescape'), '_mirror/redactions.tsv': ('path\tline\tkind\n' + ''.join((f'{p}\t{ln}\t{k}\n' for p, ln, k in redlog))).encode('utf-8', 'surrogateescape')}
    entries = sorted(((k, v[0]) for k, v in texts.items()))
    idx = tmp / 'INDEX'
    index_build(entries, idx)
    bad = index_verify({k: v for k, v in entries if Path(k).suffix.lower() in EXT_KIND}, idx)
    if bad:
        raise ValueError(f'Index declaration mismatch: {bad[:3]}')
    zp = inside(tmp, tmp / f'{name}-text.zip', 'text archive')
    with zipfile.ZipFile(zp, 'w') as z:
        for k, data in entries:
            z.writestr(_zinfo(k, texts[k][1], dt), data)
        for k, data in meta.items():
            z.writestr(_zinfo(k, 420, dt), data)
        for f in sorted(idx.iterdir()):
            z.writestr(_zinfo('INDEX/' + f.name, 420, dt), f.read_bytes())
    if zp.stat().st_size > TEXT_MAX:
        raise ValueError(f'Text archive exceeds local {TEXT_MAX} byte policy; use export-ignore for large generated files')
    out['text_zip'] = zp
    return out


def commit_files(repo: Path, files: dict[str, bytes], message: str) -> None:
    """Commit exact paths through the index, so case-only twins work on case-insensitive disks."""
    for path, data in files.items():
        sha = subprocess.run(['git', '-C', str(repo), 'hash-object', '-w', '--stdin'], input=data,
                             capture_output=True, check=True).stdout.decode().strip()
        subprocess.run(['git', '-C', str(repo), 'update-index', '--add', '--cacheinfo', f'100644,{sha},{path}'],
                       check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', message], check=True, capture_output=True)


def selftest() -> int:
    checks = 0

    def check(value, label=''):
        nonlocal checks
        assert value, label
        checks += 1
    check(name_collisions(['Foo.txt', 'foo.txt', 'caf\u00e9.md', 'cafe\u0301.md', 'other.txt'])
          == [('Foo.txt', 'foo.txt'), ('cafe\u0301.md', 'caf\u00e9.md')])
    check(reserved_paths(['Index/a.md', '_MIRROR/x', 'src/INDEX/b', 'indexer.py']) == ['Index/a.md', '_MIRROR/x'])
    with tempfile.TemporaryDirectory(prefix="mirror-build-test-") as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Sample Test")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        red = Redactor([], ["PrivateName"])
        commit_files(repo, {"Foo.txt": b"upper\n", "foo.txt": b"lower\n", "a.png": b"\0png",
                            "PrivateName/a.txt": b"x\n", "legacy.txt": "\u65e7\u6587\u672c\n".encode("gbk")}, "one")
        (root / "t1").mkdir()
        result = build(repo, "sample", "HEAD", root / "t1", red, True, True)
        check(result["collisions"] == [("Foo.txt", "foo.txt")])
        check(result["blocked"] == [("PrivateName/a.txt", "path: personal")])
        check(result["non_utf8"] == ["legacy.txt"] and result["bin_count"] == 1)
        with zipfile.ZipFile(result["text_zip"]) as z:
            check({"Foo.txt", "foo.txt", "legacy.txt"} <= set(z.namelist()))
            check(z.read("legacy.txt") == "\u65e7\u6587\u672c\n".encode("gbk"))
            check(not any("PrivateName" in n for n in z.namelist()))
        commit_files(repo, {"Index/readme.md": b"mine\n"}, "two")
        (root / "t2").mkdir()
        try:
            build(repo, "sample", "HEAD", root / "t2", red, True, False)
        except ValueError as exc:
            check("Index/readme.md" in str(exc))
        else:
            raise AssertionError("INDEX/ conflict accepted")
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
