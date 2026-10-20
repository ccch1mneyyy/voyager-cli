#!/usr/bin/env python3
"""Mirror committed Git text and optional binary assets to a local sync folder.
Use push/status --repo PATH --name NAME [--config FILE] [--out DIR].
Push accepts --assets and --force.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from code_index import EXT_KIND, build as index_build, verify as index_verify
from secret_patterns import BLOCK_PATTERNS
from kit_config import load_config, workspace_root

CHUNK = 200_000_000  # Local binary ZIP volume limit in bytes.
TEXT_MAX = 250 * 2 ** 20  # Maximum text ZIP size.
STORE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ogg", ".mp3", ".mp4", ".webm", ".zip", ".gz", ".7z", ".woff2", ".ktx2", ".basis"}
GENERIC_IDS = {"admin", "user", "users", "root", "default", "public", "guest", "shared"}
NAME_RE_T = r"^{p}-(?:text|bin)(?:-\d+of\d+)?\.zip$"

_SEG = '[^\\\\/\\s\\"\'`<>|:*?;,()\\[\\]{}]+'
HOME_PATH = [re.compile('(?<=[\\\\/])(Users[\\\\/]+)(' + _SEG + ')'), re.compile('(?<![\\w.~-])(/home/)(' + _SEG + ')')]
HOME_KEEP = {'public', 'default', 'shared', 'all users', '<user>'}
EMAIL = re.compile('(?<![\\w.+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\\.)+(?:com|net|org|cn|io|me|edu|gov|dev|app|ai|info|biz|xyz|uk|de|jp|fr|ru|kr|tw|hk|co\\.[a-z]{2})(?![\\w-])')
SECRET_ASSIGN = re.compile('(?i)((?<![A-Za-z0-9])[\\w-]*(?:api[_-]?key|secret|token|password|passwd)[\\w-]*[\\"\']?\\s*[:=]\\s*[\\"\'])([^\\"\'\\s]{12,})([\\"\'])')
PLACEHOLDER_VALUE = re.compile('(?i)paste|example|placeholder|dummy|changeme|your[_-]|local-test|fake|xxxx')
PRIVATE_KEY = re.compile(r'-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----(?:[\s\S]*?-----END (?P=kind)-----|[^\r\n]*)')
KEY_SHAPES = [(k, p) for k, p in BLOCK_PATTERNS if k not in ('email', 'win-home', 'nix-home', 'private-key')]

def _ascii_word(s: str) -> re.Pattern:
    return re.compile('(?i)(?<![A-Za-z0-9_])' + re.escape(s) + '(?![A-Za-z0-9_])')

def local_identities(repo: Path | None = None) -> list[str]:
    import getpass
    out = {getpass.getuser(), Path.home().name}
    for key in ('user.name', 'user.email'):
        try:
            v = subprocess.run(['git', '-C', str(repo or Path.cwd()), 'config', '--get', key], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            v = ''
        if v:
            out.add(v)
    return sorted((s for s in out if s))

class Redactor:

    def __init__(self, identities: list[str], extra: list[str]):
        ids = [i for i in identities if len(i) >= 5 and i.lower() not in GENERIC_IDS]
        self.words = [(w, _ascii_word(w), '<user>') for w in ids] + [(w, re.compile(re.escape(w), re.I), '<redacted>') for w in extra if w]
        self.bin_needles = sorted({x.lower() for x in identities + extra if len(x) >= 5})

    def text(self, data: bytes) -> tuple[bytes, list[tuple[int, str]]]:
        s = data.decode('utf-8', errors='surrogateescape')
        hits: list[tuple[int, str]] = []

        def rec(kind, m):
            hits.append((s_cur.count('\n', 0, m.start()) + 1, kind))
        s_cur = s
        for pat in HOME_PATH:

            def home(m):
                if m.group(2).lower() in HOME_KEEP or m.group(2)[:1] in '$%<{':
                    return m.group(0)
                rec('home-path', m)
                return m.group(1) + '<user>'
            s_cur = pat.sub(home, s_cur)
        for _w, pat, rep in self.words:

            def word(m, rep=rep):
                rec('identity' if rep == '<user>' else 'personal', m)
                return rep
            s_cur = pat.sub(word, s_cur)
        s_cur = EMAIL.sub(lambda m: (rec('email', m), '<email>')[1], s_cur)
        for kind, pat in KEY_SHAPES:
            s_cur = pat.sub(lambda m, k=kind: (rec(k, m), f'<redacted:{k}>')[1], s_cur)

        def secret(m):
            v = m.group(2)
            if v[:1] in './@$<{%' or v.lower().startswith('http') or PLACEHOLDER_VALUE.search(v):
                return m.group(0)
            rec('secret-assign', m)
            return m.group(1) + '<redacted:secret>' + m.group(3)
        s_cur = SECRET_ASSIGN.sub(secret, s_cur)
        s_cur = PRIVATE_KEY.sub(lambda m: (rec('private-key', m), '<redacted:private-key>')[1], s_cur)
        if not hits:
            return (data, [])
        return (s_cur.encode('utf-8', errors='surrogateescape'), hits)

    def binary_hit(self, data: bytes) -> bool:
        low = data.lower()
        return any((n.encode(enc) in low for n in self.bin_needles for enc in ('utf-8', 'utf-16-le', 'utf-16-be')))

def git(repo: Path, *args: str) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode('utf-8', 'replace')

def repo_root(start: Path) -> Path:
    return Path(git(start, 'rev-parse', '--show-toplevel').strip())

def read_mirror(d: Path) -> dict:
    try:
        return json.loads((d / "MIRROR.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()

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
    fp = hashlib.sha256()
    bw = ChunkWriter(tmp, f'{name}-bin') if want_bin else None
    with tarfile.open(fileobj=proc.stdout, mode='r|') as tf:
        for m in tf:
            if not m.isfile():
                continue
            data = tf.extractfile(m).read()
            if b'\x00' not in data[:8000]:
                if want_text:
                    new, hits = red.text(data)
                    redlog += [(m.name, ln, k) for ln, k in hits]
                    texts[m.name] = (new, m.mode)
                continue
            if red.binary_hit(data):
                excluded.append(m.name)
                continue
            digest = hashlib.sha1(data).hexdigest()
            fp.update(f'{m.name}\x00{digest}\n'.encode('utf-8', 'surrogateescape'))
            bins.append((m.name, len(data), digest[:12]))
            if bw:
                bw.add(m.name, data, m.mode, dt)
    if proc.wait() != 0:
        raise ValueError("git archive failed")
    out = {'commit': full, 'commit_date': cdate, 'subject': subject, 'bin_fingerprint': fp.hexdigest(), 'bin_count': len(bins), 'excluded': excluded, 'text_zip': None, 'bin_zips': bw.finish() if bw else [], 'text_count': len(texts), 'redactions': redlog}
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
    zp = tmp / f'{name}-text.zip'
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

def _layer(files: list[Path], commit: str, count: int, **extra) -> dict:
    return {'files': [f.name for f in files], 'bytes': [f.stat().st_size for f in files], 'sha256': [sha256(f) for f in files], 'file_count': count, 'commit': commit, **extra}




def mirror_dir(repo: Path, name: str | None = None, settings: dict | None = None) -> Path:
    return workspace_root(settings) / "repos" / (name or repo.name)


def _layer_ok(dest: Path, layer: dict | None) -> bool:
    return bool(layer) and all(
        (dest / name).is_file() and (dest / name).stat().st_size == size
        for name, size in zip(layer["files"], layer["bytes"])
    )


def _policy_fingerprint(red: Redactor) -> str:
    rules = [(word, pattern.pattern, replacement) for word, pattern, replacement in red.words]
    return hashlib.sha256(json.dumps([rules, red.bin_needles], sort_keys=True).encode()).hexdigest()


def push(repo: Path, name: str | None, assets: bool, force: bool, out_dir: Path | None,
         settings: dict | None = None, identities: list[str] | None = None) -> int:
    repo = repo_root(repo)
    name = (name or repo.name)
    settings = load_config() if settings is None else settings
    dest = Path(out_dir) if out_dir is not None else mirror_dir(repo, name, settings)
    dest.mkdir(parents=True, exist_ok=True)
    head = git(repo, "rev-parse", "HEAD").strip()
    prev = read_mirror(dest)
    if prev and prev.get("project") != name:
        raise ValueError("Destination contains another project's MIRROR.json; choose a separate --out")
    layers = prev.get("layers", {})
    red = Redactor(local_identities(repo) if identities is None else identities, settings.get("redact", []))
    policy = _policy_fingerprint(red)
    policy_changed = prev.get("redaction_policy") != policy
    text_layer, bin_layer = layers.get("text") or {}, layers.get("bin") or {}
    need_text = force or policy_changed or text_layer.get("commit") != head or not _layer_ok(dest, text_layer)
    need_bin = assets and (force or policy_changed or bin_layer.get("commit") != head or not _layer_ok(dest, bin_layer))
    dirty = len(git(repo, "status", "--porcelain").splitlines())
    if not need_text and not need_bin:
        print(f"Up to date: {dest} at {head[:10]}; {dirty} uncommitted changes excluded")
        return 0
    with tempfile.TemporaryDirectory(prefix="drive-mirror-") as td:
        result = build(repo, name, head, Path(td), red, want_text=need_text, want_bin=need_bin)
        layers = dict(layers)
        writes = []
        if policy_changed and not assets:
            layers.pop("bin", None)
        if need_text:
            layers["text"] = _layer([result["text_zip"]], head, result["text_count"])
            writes.append(result["text_zip"])
        old_bin = layers.get("bin")
        if need_bin and old_bin and old_bin.get("fingerprint") == result["bin_fingerprint"] and _layer_ok(dest, old_bin):
            layers["bin"] = dict(old_bin, commit=head)
            print("Binary content unchanged; retaining existing volumes")
        elif need_bin:
            layers["bin"] = _layer(result["bin_zips"], head, result["bin_count"], fingerprint=result["bin_fingerprint"])
            writes.extend(result["bin_zips"])
        if layers.get("bin"):
            layers["bin"]["same_as_text_commit"] = layers["bin"].get("fingerprint") == result["bin_fingerprint"]
        owned = re.compile(NAME_RE_T.format(p=re.escape(name)))
        for item in writes:
            target = dest / item.name
            shutil.copyfile(item, target)
            if sha256(target) != sha256(item):
                raise ValueError(f"Archive read-back checksum mismatch: {item.name}")
        keep = {filename for layer in layers.values() for filename in layer["files"]}
        for item in dest.iterdir():
            if item.is_file() and owned.fullmatch(item.name) and item.name not in keep:
                item.unlink()
        kinds = {}
        for _path, _line, kind in result["redactions"]:
            kinds[kind] = kinds.get(kind, 0) + 1
        manifest = {
            "tool": "voyager-web-pro-kit/drive-mirror/1", "project": name,
            "commit": head, "commit_date": result["commit_date"], "subject": result["subject"],
            "pushed_at": _dt.datetime.now().astimezone().isoformat(timespec="microseconds"),
            "worktree": {"uncommitted_changes_excluded": dirty}, "redaction_policy": policy, "layers": layers,
            "redactions": {"count": len(result["redactions"]), "by_kind": kinds,
                "where": "text archive: _mirror/redactions.tsv"} if need_text else prev.get("redactions"),
            "excluded_binary_files": result["excluded"],
            "how_to": ["Extract text into a new empty directory, then optionally extract binary volumes there.",
                "Check the text archive's _mirror/STAMP.json commit against MIRROR.json and the requested commit.",
                "Wait for Drive sync, then verify the files through the connected app.",
                "Binary same_as_text_commit describes content equivalence; its commit may be older.",
                "Inspect the first export before sharing."],
        }
        target = dest / "MIRROR.json"
        # This is the final published file, after archive read-back and stale-volume cleanup.
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Mirrored {head[:10]} to {dest}; wrote {len(writes)} archives; {dirty} uncommitted changes excluded")
    return 0


def status(repo: Path, name: str | None, out_dir: Path | None = None, settings: dict | None = None) -> int:
    repo = repo_root(repo)
    name = (name or repo.name)
    dest = Path(out_dir) if out_dir is not None else mirror_dir(repo, name, settings)
    head = git(repo, "rev-parse", "HEAD").strip()
    manifest = read_mirror(dest)
    layer = (manifest.get("layers") or {}).get("text") or {}
    ok = manifest.get("project") == name and layer.get("commit") == head and _layer_ok(dest, layer)
    print(f"{dest}: {'up to date' if ok else 'missing, outdated, or damaged'}; HEAD {head[:10]}")
    binary = (manifest.get("layers") or {}).get("bin")
    if binary and (binary.get("same_as_text_commit") is False or not _layer_ok(dest, binary)):
        print("Binary layer differs or is damaged; use push --assets")
    return 0 if ok else 1


def _selftest_export(root: Path) -> bool:
    repo, output, unpacked = root / "kit-source", root / "镜像", root / "unpacked"
    shutil.copytree(Path(__file__).resolve().parents[1], repo,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Sample Test"), ("add", "."), ("commit", "-qm", "sample")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    config = root / "kit-config.json"
    config.write_text("{}", encoding="utf-8")
    temporary = root / "临时文件"
    temporary.mkdir()
    # Child selftests exercise the exported scripts once, without nesting another export.
    env = dict(os.environ, VOYAGER_WEB_PRO_SELFTEST_CHILD="1", PYTHONIOENCODING="gbk",
               PYTHONUTF8="0", TEMP=str(temporary), TMP=str(temporary))
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "push", "--repo", str(repo),
         "--name", "kit", "--out", str(output), "--config", str(config)],
        env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(output / "kit-text.zip") as archive:
        archive.extractall(unpacked)
    scripts = sorted(unpacked.rglob("*.py"))
    for script in scripts:
        result = subprocess.run([sys.executable, "-B", str(script), "--selftest"], env=env,
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, f"{script.name}:\n{result.stdout}\n{result.stderr}"
    print(f"Export selftest PASS: {len(scripts)} scripts")
    return True


def selftest() -> int:
    global CHUNK
    checks = 0
    def check(value):
        nonlocal checks
        assert value
        checks += 1
    saved_chunk = CHUNK
    try:
        with tempfile.TemporaryDirectory(prefix="drive-mirror-test-") as td:
            root = Path(td)
            repo = root / "sample"
            (repo / "src").mkdir(parents=True)
            def run(*args):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            run("init", "-q")
            run("config", "user.email", "test@example.invalid")
            run("config", "user.name", "Sample Test")
            key_begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
            key_end = "-----END " + "RSA PRIVATE KEY-----"
            email = "me@" + "example.com"
            secret = "abcdefgh" + "ijklmnop"
            access_key = "AKIA" + "0123456789ABCDEF"
            source = ('# C:/Users/' + 'AliceNine/a and /home/' + 'bobfive/b and /Users/Public/c\n'
                      + f'def first():\n    token = "{secret}"\n'
                      + f'    return "PrivateName Zed12345 {email} {access_key}"\n'
                      + f'# {key_begin}\n# secret key lines\n# {key_end}\n')
            (repo / "src" / "sample.py").write_text(source, encoding="utf-8")
            tail = "following line\nlast line\n"
            (repo / "orphan.txt").write_text(key_begin + "\n" + tail, encoding="utf-8")
            (repo / "src" / "sample.ts").write_text("export function ready() { return true; }\n", encoding="utf-8")
            (repo / "one.png").write_bytes(b"\0" + b"x" * 300)
            (repo / "two.png").write_bytes(b"\0" + b"y" * 300)
            (repo / "private.bin").write_bytes(b"\0" + "Zed12345".encode("utf-16-be"))
            (repo / "skip.txt").write_text("skip", encoding="utf-8")
            (repo / ".gitattributes").write_text("skip.txt export-ignore\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-qm", "first PrivateName")
            (repo / "src" / "sample.py").write_text("uncommitted value", encoding="utf-8")
            out = root / "output"
            settings = {"redact": ["PrivateName"]}
            identities = ["Zed12345"]
            CHUNK = 350
            check(push(repo, "sample", True, False, out, settings, identities) == 0)
            first = read_mirror(out)
            head = git(repo, "rev-parse", "HEAD").strip()
            check(first["commit"] == head)
            check("PrivateName" not in first["subject"])
            check(first["layers"]["bin"]["files"] == ["sample-bin-1of2.zip", "sample-bin-2of2.zip"])
            check(first["excluded_binary_files"] == ["private.bin"])
            check(first["worktree"]["uncommitted_changes_excluded"] == 1)
            with zipfile.ZipFile(out / "sample-text.zip") as z:
                check(z.read("orphan.txt").decode().splitlines() == ["<redacted:private-key>", *tail.splitlines()])
                data = z.read("src/sample.py").decode()
                check("uncommitted value" not in data and "def first" in data)
                check("skip.txt" not in z.namelist())
                check("INDEX/INDEX-functions.tsv" in z.namelist())
                check(len(z.namelist()) == len(set(z.namelist())))
                check(json.loads(z.read("_mirror/STAMP.json"))["commit"] == head)
                for token in ("AliceNine", "bobfive", "PrivateName", "Zed12345", email, "AKIA0123", secret, "secret key lines"):
                    check(token not in data)
                check("/Users/Public/c" in data)
            check(push(repo, "sample", True, False, out, settings, identities) == 0)
            check(read_mirror(out)["pushed_at"] == first["pushed_at"])
            check(status(repo, "sample", out, settings) == 0)
            (repo / "src" / "sample.py").write_text("def updated():\n    return 2\n", encoding="utf-8")
            run("commit", "-qam", "second")
            old_time = (out / first["layers"]["bin"]["files"][0]).stat().st_mtime_ns
            check(push(repo, "sample", False, False, out, settings, identities) == 0)
            second = read_mirror(out)
            check(second["layers"]["bin"]["commit"] == head)
            check(second["layers"]["bin"]["same_as_text_commit"] is True)
            check(push(repo, "sample", True, False, out, settings, identities) == 0)
            check((out / first["layers"]["bin"]["files"][0]).stat().st_mtime_ns == old_time)
            (out / "unrelated.txt").write_text("retain", encoding="utf-8")
            (repo / "two.png").unlink()
            run("commit", "-qam", "third")
            check(push(repo, "sample", True, False, out, settings, identities) == 0)
            check(read_mirror(out)["layers"]["bin"]["files"] == ["sample-bin.zip"])
            check((out / "unrelated.txt").read_text(encoding="utf-8") == "retain")
            check(not (out / "sample-bin-1of2.zip").exists())
            prior = read_mirror(out)["pushed_at"]
            check(push(repo, "sample", False, False, out, {"redact": ["updated"]}, identities) == 0)
            check(read_mirror(out)["pushed_at"] != prior)
            with zipfile.ZipFile(out / "sample-text.zip") as z:
                check("updated" not in z.read("src/sample.py").decode())
            try:
                push(repo, "other-project", False, False, out, settings, identities)
            except ValueError:
                checks += 1
            else:
                raise AssertionError("Expected a separate project destination")
            if os.environ.get("VOYAGER_WEB_PRO_SELFTEST_CHILD") != "1":
                check(_selftest_export(root))
    finally:
        CHUNK = saved_chunk
    print(f"selftest PASS: {checks} checks")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("push", "status"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--repo", type=Path, default=Path("."))
        cmd.add_argument("--name")
        cmd.add_argument("--config")
        cmd.add_argument("--out", type=Path)
        if command == "push":
            cmd.add_argument("--assets", action="store_true")
            cmd.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load_config(args.config)
        if args.command == "status":
            return status(args.repo, args.name, args.out, settings)
        return push(args.repo, args.name, args.assets, args.force, args.out, settings)
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
