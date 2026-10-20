#!/usr/bin/env python3
"""Index exact archive bytes: --zip FILE [--must PATH::TEXT], or ROOT FILES|- OUT.
Indexes Python, JS/TS, C/C++/C# and shader declarations.
"""
from __future__ import annotations
import argparse
import re
import sys
import tempfile
import zipfile
from pathlib import Path

EXT_KIND = {".py": "indent", **{ext: "brace" for ext in
    (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".cs", ".c", ".cpp", ".h", ".hpp", ".glsl", ".shader")}}

RE_PY = re.compile('^(\\s*)(async\\s+)?def\\s+([A-Za-z_]\\w*)\\s*\\(')
RE_PY_CLASS = re.compile('^(\\s*)class\\s+([A-Za-z_]\\w*)')
RE_BRACE_FN = re.compile('^\\s*(?:export\\s+)?(?:async\\s+)?(?:function\\s+([A-Za-z_]\\w*)|(?:[A-Za-z_][\\w<>\\[\\],\\s\\*&]*\\s+)([A-Za-z_]\\w*)\\s*\\([^;]*\\)\\s*(?:const\\s*)?\\{?\\s*$|(?:const|let|var)\\s+([A-Za-z_]\\w*)\\s*=\\s*(?:async\\s*)?\\([^)]*\\)\\s*=>)')
RE_BRACE_CLASS = re.compile('^\\s*(?:export\\s+)?(?:public\\s+|private\\s+|internal\\s+)?class\\s+([A-Za-z_]\\w*)')
KEYWORDS = {'if', 'for', 'while', 'switch', 'return', 'else', 'catch', 'do', 'sizeof'}

def index_indent(lines: list[str]) -> list[tuple]:
    defs = []
    for i, ln in enumerate(lines):
        m = RE_PY.match(ln)
        if m:
            name = m.group(3)
            defs.append([len(m.group(1).expandtabs(4)), name, 'func', i])
            continue
        m = RE_PY_CLASS.match(ln)
        if m:
            defs.append([len(m.group(1).expandtabs(4)), m.group(2), 'class', i])
    out = []
    for k, (ind, name, kind, start) in enumerate(defs):
        end = len(lines) - 1
        for ind2, _, _, s2 in defs[k + 1:]:
            if ind2 <= ind:
                end = s2 - 1
                break
        while end > start and (not lines[end].strip()):
            end -= 1
        out.append((name, kind, start + 1, end + 1, lines[start].strip()[:160]))
    return out

def index_brace(lines: list[str]) -> list[tuple]:
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = RE_BRACE_CLASS.match(ln)
        kind, name = ('class', m.group(1)) if m else (None, None)
        if not kind:
            m = RE_BRACE_FN.match(ln)
            if m:
                name = next((g for g in m.groups() if g), None)
                kind = 'func' if name and name not in KEYWORDS else None
        if not kind:
            i += 1
            continue
        depth, opened, j = (0, False, i)
        while j < len(lines):
            for ch in lines[j]:
                if ch == '{':
                    depth += 1
                    opened = True
                elif ch == '}':
                    depth -= 1
            if opened and depth <= 0:
                break
            if not opened and j > i + 3:
                break
            j += 1
        if opened:
            out.append((name, kind, i + 1, min(j, len(lines) - 1) + 1, ln.strip()[:160]))
        i += 1
    return out

def build(entries: list[tuple[str, bytes]], outdir: Path) -> tuple[int, int]:
    outdir.mkdir(parents=True, exist_ok=True)
    frows, nfn = ([], 0)
    with open(outdir / 'INDEX-functions.tsv', 'w', encoding='utf-8', newline='') as fn:
        fn.write('path\tname\tkind\tstart\tend\tsignature\n')
        for rel, data in entries:
            text = data.decode('utf-8', errors='replace')
            lines = text.splitlines()
            frows.append((rel, len(lines), len(data)))
            suffix = Path(rel).suffix.lower()
            kind = EXT_KIND.get(suffix)
            if not kind:
                continue
            defs = index_indent(lines) if kind == 'indent' else index_brace(lines)
            for name, k, s, e, sig in defs:
                fn.write(f"{rel}\t{name}\t{k}\t{s}\t{e}\t{sig.replace(chr(9), ' ')}\n")
                nfn += 1
    with open(outdir / 'INDEX-files.tsv', 'w', encoding='utf-8', newline='') as ff:
        ff.write('path\tlines\tbytes\n')
        for rel, n, b in frows:
            ff.write(f'{rel}\t{n}\t{b}\n')
    (outdir / 'INDEX-README.md').write_text('Index built from the archived bytes. File table: paths, lines, bytes. Definition table: names, line ranges, signatures. Use these tables to locate declarations in the source.', encoding='utf-8')
    return (len(frows), nfn)

def run(root: Path, files: list[str], outdir: Path) -> tuple[int, int]:
    entries = [(rel, (root / rel).read_bytes()) for rel in files if (root / rel).is_file()]
    return build(entries, outdir)

def verify(entries: dict[str, bytes], outdir: Path) -> list[str]:
    bad = []
    cache: dict[str, list[str]] = {}
    for row in (outdir / 'INDEX-functions.tsv').read_text(encoding='utf-8').splitlines()[1:]:
        rel, name, _k, start, *_ = row.split('\t')
        lines = cache.setdefault(rel, entries[rel].decode('utf-8', errors='replace').splitlines())
        i = int(start) - 1
        if not (0 <= i < len(lines) and name in lines[i]):
            bad.append(f'{rel}:{start} {name}')
    return bad


def run_zip(zpath: Path, musts: list[str]) -> int:
    with zipfile.ZipFile(zpath) as z:
        names = [name for name in z.namelist() if not name.endswith("/")]
        if any(name.startswith("INDEX/") for name in names):
            print("ERROR: Archive already has INDEX/; refusing duplicate entries")
            return 2
        if len(set(names)) != len(names):
            print("ERROR: Archive has duplicate entries")
            return 2
        entries = {name: z.read(name) for name in names}
    for requirement in musts:
        rel, separator, snippet = requirement.partition("::")
        if not separator or rel not in entries or snippet not in entries[rel].decode("utf-8", errors="replace"):
            print(f"ERROR: Required archived text not found: {requirement}")
            return 1
    with tempfile.TemporaryDirectory(prefix="code-index-") as td:
        out = Path(td)
        nfile, nfn = build(sorted(entries.items()), out)
        bad = verify(entries, out)
        if bad:
            print(f"ERROR: Index declaration mismatch: {bad[:5]}")
            return 1
        with zipfile.ZipFile(zpath, "a", compression=zipfile.ZIP_DEFLATED) as z:
            for item in sorted(out.iterdir()):
                z.write(item, "INDEX/" + item.name)
    print(f"indexed {nfile} files, {nfn} definitions -> {zpath}")
    return 0


def selftest() -> int:
    checks = 0
    def check(value):
        nonlocal checks
        assert value
        checks += 1
    with tempfile.TemporaryDirectory(prefix="code-index-test-") as td:
        root = Path(td)
        entries = {"a.py": b"class Example:\n    def method(self):\n        return 1\n\ndef tail():\n    return 2\n",
                   "b.ts": b"export function first() {\n  return 1;\n}\nexport const second = () => {\n  return 2;\n};\n",
                   "notes.md": b"required plain text\n"}
        out = root / "index"
        check(build(sorted(entries.items()), out) == (3, 5))
        rows = [line.split("\t") for line in (out / "INDEX-functions.tsv").read_text(encoding="utf-8").splitlines()[1:]]
        check({row[1] for row in rows} == {"Example", "method", "tail", "first", "second"})
        check(next(row for row in rows if row[1] == "method")[3:5] == ["2", "3"])
        check(not verify(entries, out))
        changed = dict(entries, **{"a.py": b"# changed\n"})
        check(bool(verify(changed, out)))
        zp = root / "archive.zip"
        with zipfile.ZipFile(zp, "w") as z:
            for name, data in entries.items():
                z.writestr(name, data)
        check(run_zip(zp, ["notes.md::required plain text"]) == 0)
        with zipfile.ZipFile(zp) as z:
            check("INDEX/INDEX-files.tsv" in z.namelist())
            check(len(z.namelist()) == len(set(z.namelist())))
        check(run_zip(zp, []) == 2)
        empty = root / "empty.zip"
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("notes.md", "other text")
        before = empty.read_bytes()
        check(run_zip(empty, ["notes.md::missing"]) == 1)
        check(empty.read_bytes() == before)
    print(f"selftest PASS: {checks} checks")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--must", action="append", default=[])
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if args.zip:
        if args.paths:
            parser.error("--zip does not accept positional paths")
        return run_zip(args.zip, args.must)
    if len(args.paths) != 3 or args.must:
        parser.error("Use ROOT FILES|- OUT, or --zip FILE [--must PATH::TEXT]")
    root, flist, outdir = Path(args.paths[0]), args.paths[1], Path(args.paths[2])
    if flist == "-":
        files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.suffix.lower() in EXT_KIND)
    else:
        files = [line.split("\t")[0] for line in Path(flist).read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    nfile, nfn = run(root, files, outdir)
    print(f"indexed {nfile} files, {nfn} definitions -> {outdir}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
