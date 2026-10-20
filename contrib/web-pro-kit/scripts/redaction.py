#!/usr/bin/env python3
"""Redact recognisable private text and decide which files must stay local.
scan() returns text (redacted), binary, or blocked with a rule name; it never reports matched values.
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple
from secret_patterns import BLOCK_PATTERNS

RULES_VERSION = 2  # Part of the published policy fingerprint: raise it when rules change.
GENERIC_IDS = {"admin", "user", "users", "root", "default", "public", "guest", "shared"}
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".cs", ".c", ".cpp", ".cc", ".h", ".hpp", ".java",
            ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".swift", ".scala", ".m", ".mm", ".lua", ".dart", ".vue",
            ".svelte", ".glsl", ".shader", ".gd", ".groovy", ".r", ".jl", ".pl", ".ex", ".exs", ".hs", ".fs", ".vb"}

_SEG = '[^\\\\/\\s\\"\'`<>|:*?;,()\\[\\]{}]+'
HOME_PATH = [re.compile('(?<=[\\\\/])(Users[\\\\/]+)(' + _SEG + ')'), re.compile('(?<![\\w.~-])(/home/)(' + _SEG + ')')]
HOME_KEEP = {'public', 'default', 'shared', 'all users', '<user>'}
EMAIL = re.compile('(?<![\\w.+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\\.)+(?:com|net|org|cn|io|me|edu|gov|dev|app|ai|info|biz|xyz|uk|de|jp|fr|ru|kr|tw|hk|co\\.[a-z]{2})(?![\\w-])')
KEY_SHAPES = [(k, p) for k, p in BLOCK_PATTERNS if k not in ('email', 'win-home', 'nix-home', 'private-key')]
PRIVATE_KEY = re.compile(r'-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----[\s\S]*?-----END (?P=kind)-----')
PRIVATE_KEY_OPEN = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')

_WORD = r'(?:api[_-]?key|secret|token|password|passwd|private[_-]?key|access[_-]?key)'
# Quoted literal after a sensitive key: JSON, YAML, dotenv, code (with optional type annotation).
QUOTED_ASSIGN = re.compile(r'''(?i)(?<![A-Za-z0-9])(?P<key>[\w-]*%s[\w-]*)(?P<sep>["']?\]?\s*(?::\s*[A-Za-z_][\w\[\]., |]*?\s*)?(?:=>|:=|=|:)\s*)(?P<q>["'`])(?P<val>(?:\\.|(?!(?P=q))[^\\\r\n])*)(?P=q)''' % _WORD)
# Unquoted value at the start of a line: .env, export/set, YAML, INI. Not applied to code files.
UNQUOTED_ASSIGN = re.compile(r'''(?im)^(?P<pre>[ \t]*(?:-[ \t]+)?(?:(?:export|set|ENV|ARG)[ \t]+)?["']?)(?P<key>[\w.-]+)(?P<sep>["']?[ \t]*[:=][ \t]*)(?P<val>[^\s"'`][^\r\n]*?)(?P<tail>[ \t]+\#[^\r\n]*|[ \t]*)(?=\r?$)''')
PLACEHOLDER = re.compile(r'(?i)^(?:x{3,}|\*{3,}|\.{3}|…|changeme|change[_-]me|placeholder|example|dummy|fake|todo|tbd|none|null|nil|undefined|true|false|secret|password|passwd|token|redacted|your[_-][a-z][a-z_-]*|<[^<>\r\n]*>)$')
VAR_REF = re.compile(r'''^(?:\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%|\$?\{\{\s*[\w.\[\]'"-]+\s*\}\}|\{[A-Za-z_]\w*\}|\$env:[A-Za-z_]\w*|process\.env\.[A-Za-z_]\w*|process\.env\[["'][\w-]+["']\]|os\.environ\[["'][\w-]+["']\]|os\.environ\.get\(["'][\w-]+["']\)|os\.getenv\(["'][\w-]+["']\))$''')
# Unquoted right-hand sides that are code, not literals: calls, attribute paths, lookups, YAML syntax.
NON_LITERAL = re.compile(r'''^(?:\$\(.*\)|`.*`|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\(.*\)|[A-Za-z_$][A-Za-z_$]*(?:\.[A-Za-z_$][A-Za-z_$]*)+|[A-Za-z_$][\w$.]*\[["']?[\w-]+["']?\]|![A-Za-z][^\r\n]*|[|>][+-]?\d*|[{\[]|\{\}|\[\])$''')
CONCAT = re.compile(r'[ \t]*\+')
TYPE_NAMES = {'string', 'str', 'number', 'int', 'integer', 'float', 'bool', 'boolean', 'any', 'unknown', 'object', 'bytes'}
_KEY_PART = re.compile(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+')
RAW_ENCODINGS = ('utf-8', 'gb18030', 'utf-16-le', 'utf-16-be')


class Scan(NamedTuple):
    kind: str  # "text", "binary" or "blocked"
    data: bytes | None
    hits: list
    reason: str | None = None
    note: str | None = None  # "utf-16" or "non-utf8" for text kept in its own encoding


def _ascii_word(s: str) -> re.Pattern:
    return re.compile('(?i)(?<![A-Za-z0-9_])' + re.escape(s) + '(?![A-Za-z0-9_])')


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def local_identities(repo: Path | None = None, warn=_warn) -> list[str]:
    import getpass
    out = set()
    try:
        out.add(getpass.getuser())
    except Exception as exc:
        warn(f"cannot read the login name ({type(exc).__name__}); add it to redact")
    try:
        out.add(Path.home().name)
    except RuntimeError:
        warn("cannot find the home folder name; add it to redact")
    for key in ('user.name', 'user.email'):
        try:
            r = subprocess.run(['git', '-C', str(repo or Path.cwd()), 'config', '--get', key], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            warn(f"cannot run git config {key} ({type(exc).__name__}); it is not auto-redacted, add it to redact")
            continue
        if r.returncode == 1 and not r.stdout.strip():
            continue  # Not configured.
        try:
            # Git stores config values as UTF-8 on every platform; the locale may differ.
            v = r.stdout.decode('utf-8').strip() if r.returncode == 0 else ''
        except UnicodeDecodeError:
            v = None
        if not v:
            warn(f"cannot read git config {key} as UTF-8; it is not auto-redacted, add it to redact")
            continue
        out.add(v)
    return sorted(s for s in out if s)


def _strict_key(key: str) -> bool:
    """True when the key names the secret itself (API_KEY, client_secret), not metadata (token_url)."""
    parts = [p.lower() for p in _KEY_PART.findall(key)]
    while parts and (parts[-1].isdigit() or parts[-1] == 'v'):
        parts.pop()
    if not parts:
        return False
    if parts[-1] in {'secret', 'token', 'password', 'passwd', 'apikey', 'accesskey', 'privatekey'}:
        return True
    return parts[-1] == 'key' and len(parts) > 1 and parts[-2] in {'api', 'secret', 'private', 'access'}


def _exempt(value: str) -> bool:
    v = value.strip()
    return not v or bool(PLACEHOLDER.match(v) or VAR_REF.match(v))


def utf16_encoding(data: bytes) -> tuple[str, bytes] | None:
    """Return (codec, BOM) when data is UTF-16 text: a BOM, or NULs on one side and printable text."""
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        codec, bom = ('utf-16-le' if data[:2] == b'\xff\xfe' else 'utf-16-be'), data[:2]
    else:
        sample = data[:8000][:len(data[:8000]) // 2 * 2]
        half = len(sample) // 2
        even, odd = sample[0::2].count(0), sample[1::2].count(0)
        if half < 2 or len(data) % 2:
            return None
        if odd >= 0.6 * half and even == 0:
            codec, bom = 'utf-16-le', b''
        elif even >= 0.6 * half and odd == 0:
            codec, bom = 'utf-16-be', b''
        else:
            return None
    try:
        s = data[len(bom):].decode(codec)
    except UnicodeDecodeError:
        return None
    # Text has no control characters besides whitespace; misread binary assets almost always do.
    return None if re.search('[\x00-\x08\x0e-\x1f\x7f-\x9f]', s) else (codec, bom)


class Redactor:

    def __init__(self, identities: list[str], extra: list[str]):
        ids = [i for i in identities if len(i) >= 5 and i.lower() not in GENERIC_IDS]
        self.words = [(w, _ascii_word(w), '<user>') for w in ids] + [(w, re.compile(re.escape(w), re.I), '<redacted>') for w in extra if w]
        self.bin_needles = sorted({x.lower() for x in identities + extra if len(x) >= 5})
        # Configured terms count at any length (张三); auto-detected identities need five characters.
        self.raw_needles = sorted({(w.lower(), 'identity') for w in ids} | {(w.lower(), 'personal') for w in extra if w})

    def redact(self, s: str, code: bool = False) -> tuple[str, list[tuple[int, str]], bool]:
        """Return (text, [(line, kind)], unclosed_private_key)."""
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

        def quoted(m):
            v = m.group('val')
            # Fragments joined with + form an expression, not a literal. Like metadata keys (token_url),
            # they keep the original rule: a quoted value of 12+ characters without spaces.
            strict = _strict_key(m.group('key')) and not CONCAT.match(m.string, m.end())
            if _exempt(v) or not (strict or (len(v) >= 12 and not re.search(r'[\s"\']', v))):
                return m.group(0)
            rec('secret-assign', m)
            return m.group('key') + m.group('sep') + m.group('q') + '<redacted:secret>' + m.group('q')
        s_cur = QUOTED_ASSIGN.sub(quoted, s_cur)

        def unquoted(m):
            v = m.group('val')
            body = v.rstrip(',;')
            if (not _strict_key(m.group('key')) or _exempt(body) or NON_LITERAL.match(body)
                    or body.lower() in TYPE_NAMES):
                return m.group(0)
            rec('secret-assign', m)
            return m.group('pre') + m.group('key') + m.group('sep') + '<redacted:secret>' + v[len(body):] + m.group('tail')
        if not code:
            s_cur = UNQUOTED_ASSIGN.sub(unquoted, s_cur)
        s_cur = PRIVATE_KEY.sub(lambda m: (rec('private-key', m), '<redacted:private-key>')[1], s_cur)
        return s_cur, hits, bool(PRIVATE_KEY_OPEN.search(s_cur))

    def text(self, data: bytes) -> tuple[bytes, list[tuple[int, str]]]:
        new, hits, _open = self.redact(data.decode('utf-8', errors='surrogateescape'))
        return (new.encode('utf-8', errors='surrogateescape'), hits) if hits else (data, [])

    def binary_hit(self, data: bytes) -> bool:
        low = data.lower()
        return any((n.encode(enc) in low for n in self.bin_needles for enc in ('utf-8', 'utf-16-le', 'utf-16-be')))

    def path_hit(self, path: str) -> str | None:
        for _w, pat, rep in self.words:
            if pat.search(path):
                return 'identity' if rep == '<user>' else 'personal'
        return None

    def raw_hits(self, data: bytes) -> list[str]:
        """Rule names found in undecodable text: terms in four encodings, ASCII credential patterns."""
        low = data.lower()
        kinds = {kind for needle, kind in self.raw_needles for enc in RAW_ENCODINGS if needle.encode(enc) in low}
        _s, hits, unclosed = self.redact(data.decode('latin-1'))
        kinds.update(k for _ln, k in hits)
        if unclosed:
            kinds.add('private-key')
        return sorted(kinds)

    def scan(self, path: str, data: bytes) -> Scan:
        found = self.path_hit(path)
        if found:
            return Scan('blocked', None, [], f'path: {found}')
        code = Path(path).suffix.lower() in CODE_EXT
        wide = utf16_encoding(data) if b'\x00' in data[:8000] else None
        if b'\x00' in data[:8000] and not wide:
            if self.binary_hit(data):
                return Scan('blocked', None, [], 'binary: private term or identity')
            return Scan('binary', data, [])
        codec, bom = wide or ('utf-8', b'')
        try:
            s = data[len(bom):].decode(codec)
        except UnicodeDecodeError:
            # Never rewrite bytes in an unknown encoding: GBK trail bytes can be ASCII letters.
            kinds = self.raw_hits(data)
            if kinds:
                return Scan('blocked', None, [], 'non-UTF-8 text: ' + ', '.join(kinds))
            return Scan('text', data, [], note='non-utf8')
        new, hits, unclosed = self.redact(s, code)
        if unclosed:
            return Scan('blocked', None, [], 'unclosed private key')
        return Scan('text', bom + new.encode(codec) if hits else data, hits, note='utf-16' if wide else None)


def selftest() -> int:
    checks = 0

    def check(value, label=''):
        nonlocal checks
        assert value, label
        checks += 1
    names = {'{P}': 'pass' + 'word', '{PU}': 'PASS' + 'WORD', '{T}': 'to' + 'ken', '{TU}': 'TO' + 'KEN',
             '{S}': 'sec' + 'ret', '{SU}': 'SEC' + 'RET', '{K}': 'API_' + 'KEY'}

    def f(text):
        for k, v in names.items():
            text = text.replace(k, v)
        return text
    red = Redactor(['Zed12345'], ['PrivateName', '张三'])
    leaks = [  # (line, code file?) - every value contains Zq7 and must disappear.
        ('{K}=Zq7short', False), ('export GITHUB_{TU}=Zq7tok', False), ('{P}: Zq7yaml', False),
        ('DB_{PU}="Zq7 has spaces"', False), ("{P}='Zq7'", True), ('{P}="@Zq7first"', True),
        ('{T} = "Zq7examplefakedummy"', True), ('{S}: https://user:Zq7@host.invalid', False),
        ('"{K}": "Zq7json",', False), ('  - POSTGRES_{PU}=Zq7compose', False), ('{P} = Zq7 # note', False),
        ('{TU}=Zq7crlf\r\nNEXT=1\r\n', False), ('self.{T} = "Zq7code"', True), ('{P}: str = "Zq7typed"', True),
        ('{TU}=${HOME}Zq7', False), ('set {PU}=Zq7win', False), ('client_{S}: Zq7client', False),
        ('os.environ["{K}"] = "Zq7env"', True), ('{K}=Zq7', False), ('{P}: correct horse Zq7', False),
    ]
    for line, code in leaks:
        out, hits, _ = red.redact(f(line), code)
        check('Zq7' not in out and ('secret-assign' in {k for _l, k in hits}), f'leak: {line!r} -> {out!r}')
        again = red.redact(out, code)
        check(not again[1], f'not idempotent: {out!r}')
    keep = [  # Non-literals, placeholders and metadata keys stay untouched.
        ('{T} = getToken()', True), ('{T} = getToken()', False), ('{P}: string', True), ('{P}: string', False),
        ('{S}_key = settings.{SU}_KEY', True), ('{S}_key = settings.{SU}_KEY', False), ('{TU}=${{TU}}', False),
        ('{P}=changeme', False), ('{K}=<your-key>', False), ('{K}=xxxxxxxx', False), ('{P}=', False),
        ("{T}: '{{ vault_{T} }}'", False), ('{P}=%DB_PASS%', False), ('{T}: process.env.GH', False),
        ('{T} = os.environ["GH"]', False), ('max_{T}s: 4096', False), ('{T}_url: https://example.invalid/x', False),
        ('{T}izer = "bert-base"', True), ('<input type="{P}" name="{P}">', False), ('{K}: ${{ secrets.X }}', False),
        ('"{T}Expired": "Your session {T} expired"', False), ('{TU}=$(cat ./file)', False), ('{T}: |', False),
        ('{P} = "{pw}"', True), ('if {T} == "abc":', True), ('{P} = input()', False),
        ('{S} = "abcd" + "efgh"', True),
    ]
    for line, code in keep:
        text = f(line)
        out, hits, _ = red.redact(text, code)
        check(out == text and not hits, f'false positive: {text!r} -> {out!r}')
    begin, end = '-----BEGIN ' + 'RSA PRIVATE KEY-----', '-----END ' + 'RSA PRIVATE KEY-----'
    closed = red.scan('a.txt', f'x\n{begin}\nAAAA\n{end}\ny\n'.encode())
    check(closed.kind == 'text' and b'AAAA' not in closed.data and b'<redacted:private-key>' in closed.data)
    check(red.scan('k.txt', f'{begin}\nAAAA\n'.encode()) == Scan('blocked', None, [], 'unclosed private key'))
    for codec in ('utf-16', 'utf-16-le', 'utf-16-be'):
        data = f('{K}=Zq7wide\r\nname=ok\r\n').encode(codec)
        result = red.scan('cfg.ini', data)
        check(result.kind == 'text' and result.note == 'utf-16', codec)
        check('Zq7' not in result.data.decode(codec) and 'name=ok' in result.data.decode(codec), codec)
        check(result.data[:2] == data[:2])  # The BOM, when present, is kept.
    png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + bytes(range(256))
    check(red.scan('a.png', png) == Scan('binary', png, []))
    check(red.scan('b.bin', b'\0' + 'Zed12345'.encode('utf-16-be')).kind == 'blocked')
    gbk_hit, gbk_clean = '作者：张三\n'.encode('gbk'), '普通的说明文字\n'.encode('gbk')
    for data in (gbk_hit, gbk_clean):
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            checks += 1
        else:
            raise AssertionError('GBK fixture decodes as UTF-8')
    check(red.scan('notes.txt', gbk_hit) == Scan('blocked', None, [], 'non-UTF-8 text: personal'))
    check(red.scan('notes.txt', gbk_clean) == Scan('text', gbk_clean, [], note='non-utf8'))
    check(red.scan('cfg.txt', f('{P}=Zq7\n说明\n').encode('gbk')).reason == 'non-UTF-8 text: secret-assign')
    check(red.scan('docs/PrivateName-notes.txt', b'hello').reason == 'path: personal')
    check(red.scan('docs/张三.txt', b'hello').reason == 'path: personal')
    check(red.scan('zed12345/a.txt', b'hello').reason == 'path: identity')
    check(red.scan('docs/notes.txt', b'hello') == Scan('text', b'hello', []))
    warnings = []
    ids = local_identities(Path(__file__).resolve().parent, warnings.append)
    check(all(isinstance(i, str) and i for i in ids))
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
