#!/usr/bin/env python3
"""Remind the coordinator to check the task\'s Drive request file."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

VERSION = 2
INTERVAL = 900  # Soft fallback: 15 minutes
ALERT_INTERVAL = 900  # Avoid repeating the same reminder every command
BEGIN = "# >>> REQUEST_CHANNEL_MANAGED >>>"
END = "# <<< REQUEST_CHANNEL_MANAGED <<<"


def home_dir():
    return Path(os.environ.get("REQUEST_CHANNEL_HOME") or (Path.home() / ".request-channel")).expanduser().resolve()


def utc_string(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validated_mtime(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        return value  # Preserve Drive's exact string for direct modified_time equality checks.
    except (ValueError, TypeError) as exc:
        raise ValueError("--mtime requires an ISO-8601 timestamp with timezone, e.g. 2026-01-15T09:00:00.000Z") from exc


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent), prefix=".channel-", delete=False) as temp:
        name = temp.name
        try:
            temp.write(data.encode("utf-8") if isinstance(data, str) else data)
            temp.flush()
            os.fsync(temp.fileno())
        except Exception:
            os.unlink(name)
            raise
    try:
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_int(path):
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        if not value or not re.fullmatch(r"-?[0-9]+", value):
            return None
        return int(value)
    except (OSError, UnicodeError):
        return None


def read_state(root):
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("version"), int) or not isinstance(state.get("mtime"), str) or not isinstance(state.get("checked_epoch"), int):
        raise ValueError("invalid state.json; check files or use a fresh REQUEST_CHANNEL_HOME")
    return state


def overdue(root, now=None):
    if now is None:
        now = int(time.time())
    stamp = read_int(root / "last-check")
    if stamp is None:
        return True, "never_checked", 0
    age = now - stamp
    if age < -60:
        return True, "clock_went_backwards", age
    if age >= INTERVAL:
        return True, "elapsed_15m", age
    return False, "not_due", age


def reminder(root, output=None, now=None):
    """Emits at most one line, throttled across Bash and IPython processes."""
    if output is None:
        output = sys.stderr
    if now is None:
        now = int(time.time())
    due, reason, age = overdue(root, now)
    if not due:
        return False
    last_alert = read_int(root / "last-alert")
    if last_alert is not None and 0 <= now - last_alert < ALERT_INTERVAL:
        return False
    try:
        atomic_write(root / "last-alert", f"{now}\n")
    except OSError:
        pass  # Reminder is more important than persisting its throttle marker.
    print(f"CHANNEL_CHECK_DUE reason={reason} age_s={age}", file=output)
    return True


def record(root, version, mtime, now=None):
    if version < 0:
        raise ValueError("version must be non-negative")
    mtime = validated_mtime(mtime)
    prior = read_state(root)
    if prior is not None:
        if version < prior["version"]:
            raise ValueError("version rollback rejected")
        if version == prior["version"]:
            previous_mtime = validated_mtime(prior["mtime"])
            prev_time = dt.datetime.fromisoformat(previous_mtime.replace("Z", "+00:00"))
            new_time = dt.datetime.fromisoformat(mtime.replace("Z", "+00:00"))
            if prev_time != new_time:
                raise ValueError("same-version modified time changed: wait for a higher request version")
    now = int(time.time()) if now is None else int(now)
    state = {"version": version, "mtime": mtime, "checked_epoch": now}
    atomic_write(root / "state.json", json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n")
    atomic_write(root / "last-check", f"{now}\n")
    with contextlib.suppress(OSError):
        (root / "last-alert").unlink()
    return state


def bash_hook(root):
    # Bash builtins read the local reminder timestamps.
    r = str(root).replace("'", "'\\''")
    return f'''# Managed request-channel hook. Sourced by BASH_ENV or login profile.
__rc_root=''' + "'" + r + "'" + r'''
__rc_last=''
if [[ -r "$__rc_root/last-check" ]]; then IFS= read -r __rc_last < "$__rc_root/last-check" || :; fi
if ! printf -v __rc_now '%(%s)T' -1 2>/dev/null || [[ ! "$__rc_now" =~ ^[0-9]+$ ]]; then
  __rc_now=$(date +%s 2>/dev/null || printf '0')
fi
__rc_due=0; __rc_reason=not_due; __rc_age=0
if [[ ! "$__rc_last" =~ ^[0-9]+$ ]]; then
  __rc_due=1; __rc_reason=never_checked
else
  __rc_age=$((__rc_now - __rc_last))
  if (( __rc_age < -60 )); then __rc_due=1; __rc_reason=clock_went_backwards
  elif (( __rc_age >= 900 )); then __rc_due=1; __rc_reason=elapsed_15m
  fi
fi
if (( __rc_due )); then
  __rc_alert=''
  if [[ -r "$__rc_root/last-alert" ]]; then IFS= read -r __rc_alert < "$__rc_root/last-alert" || :; fi
  if [[ ! "$__rc_alert" =~ ^[0-9]+$ ]] || (( __rc_now - __rc_alert >= 900 || __rc_now < __rc_alert )); then
    printf '%s\n' "$__rc_now" > "$__rc_root/last-alert" 2>/dev/null || :
    printf 'CHANNEL_CHECK_DUE reason=%s age_s=%s\n' "$__rc_reason" "$__rc_age" >&2
  fi
fi
unset __rc_root __rc_last __rc_now __rc_due __rc_reason __rc_age __rc_alert
'''


def register_ipython_hook(ip, root=None):
    """Called by IPython startup file. Also supports explicit activation in a live kernel."""
    if ip is None or not hasattr(ip, "events"):
        return False
    root = home_dir() if root is None else Path(root)
    marker = "_request_channel_pre_run_cell_hook"
    old = getattr(ip, marker, None)
    if old is not None:
        with contextlib.suppress(Exception):
            ip.events.unregister("pre_run_cell", old)

    def _hook(_info):
        with contextlib.suppress(Exception):
            reminder(root)

    ip.events.register("pre_run_cell", _hook)
    setattr(ip, marker, _hook)
    return True


def startup_file(script_path):
    return ("# Auto-created by request-channel; triggers only before IPython cells.\n"
            "import runpy as _request_channel_runpy\n"
            "try:\n"
            f"    _request_channel_ns = _request_channel_runpy.run_path({str(script_path)!r})\n"
            "    _request_channel_ns['register_ipython_hook'](get_ipython())\n"
            "except Exception as _request_channel_error:\n"
            "    import sys as _request_channel_sys\n"
            "    print('CHANNEL_IPYTHON_INSTALL_FAILED ' + str(_request_channel_error), file=_request_channel_sys.stderr)\n")


def install_profile(path, block):
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    old_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), flags=re.DOTALL)
    if BEGIN in old and END not in old:
        raise RuntimeError(f"unclosed existing managed block in {path}")
    if BEGIN in old:
        new = pattern.sub(lambda _match: block, old)
    else:
        new = old.rstrip("\n") + "\n\n" + block + "\n"
    if new != old:
        atomic_write(path, new)
        os.chmod(path, old_mode)


def cmd_install(args):
    home = Path(args.sandbox_home).expanduser().resolve()
    root = Path(os.environ.get("REQUEST_CHANNEL_HOME") or (home / ".request-channel"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"INSTALL bash=failed ipython=failed reason={type(exc).__name__}:{exc}")
        return 0
    bash_status = "skipped"
    ipy_status = "skipped"
    try:
        managed_script = root / "channel.py"
        if Path(__file__).resolve() != managed_script.resolve():
            atomic_write(managed_script, Path(__file__).read_bytes())
        else:
            managed_script = Path(__file__).resolve()
    except Exception as exc:
        managed_script = Path(__file__).resolve()
        print(f"INSTALL_COPY_WARNING reason={type(exc).__name__}:{exc}")
    if shutil.which("bash"):
        try:
            hook = root / "bash_env.sh"
            atomic_write(hook, bash_hook(root))
            login = next((p for p in (home / ".bash_profile", home / ".bash_login", home / ".profile") if p.exists()), home / ".profile")
            block = (BEGIN + "\n" + f"export BASH_ENV={shlex.quote(str(hook))}\n" + '[ -r "$BASH_ENV" ] && . "$BASH_ENV"\n' + END)
            install_profile(login, block)
            install_profile(home / ".bashrc", block)
            bash_status = f"ok(login={login.name},interactive=.bashrc)"
        except Exception as exc:
            bash_status = f"failed({type(exc).__name__}:{exc})"
    else:
        bash_status = "skipped(bash_not_found)"
    try:
        ipy_dir = home / ".ipython"
        startup = ipy_dir / "profile_default" / "startup" / "00_request_channel.py"
        atomic_write(startup, startup_file(managed_script))
        ipy_status = "ok(next_IPython_session;live_kernel_requires_activation)"
    except Exception as exc:
        ipy_status = f"failed({type(exc).__name__}:{exc})"
    print(f"INSTALL bash={bash_status} ipython={ipy_status} state_dir={root}")
    return 0  # Continue with checkpoint checks when a hook fails.


def cmd_record(args):
    try:
        state = record(home_dir(), args.version, args.mtime)
        print(f"RECORDED version={state['version']} mtime={state['mtime']} checked_utc={utc_string(state['checked_epoch'])}")
        return 0
    except Exception as exc:
        print(f"ERROR record {exc}")
        return 2


def cmd_due(_args):
    due, reason, age = overdue(home_dir())
    if due:
        print(f"DUE reason={reason} age_s={age}")
    else:
        print(f"NOT_DUE remaining_s={max(0, INTERVAL - age)}")
    return 0


def cmd_status(_args):
    try:
        state = read_state(home_dir())
        due, reason, age = overdue(home_dir())
        if state is None:
            print("STATUS version=- checked_utc=- mtime=- due=yes reason=never_checked")
        else:
            print(f"STATUS version={state['version']} checked_utc={utc_string(state['checked_epoch'])} mtime={state['mtime']} due={'yes' if due else 'no'} reason={reason}")
        return 0
    except Exception as exc:
        print(f"STATUS invalid_state reason={exc}")
        return 2


def cmd_selftest(_args):
    try:
        with tempfile.TemporaryDirectory(prefix="request-channel-selftest-") as temp:
            root = Path(temp)
            t = int(time.time())
            assert overdue(root, t)[0] and overdue(root, t)[1] == "never_checked"
            state = record(root, 2, "2026-01-15T09:00:00.000Z", t)
            assert state["version"] == 2 and not overdue(root, t)[0]
            assert read_state(root)["mtime"] == "2026-01-15T09:00:00.000Z"
            record(root, 2, "2026-01-15T09:00:00Z", t)  # Equivalent timestamp representation
            try:
                record(root, 2, "2026-01-15T09:01:00Z", t)
            except ValueError as exc:
                assert "same-version" in str(exc)
            else:
                raise AssertionError("uncommitted same-version mtime was accepted")
            test = io.StringIO()
            assert not reminder(root, test, t) and test.getvalue() == ""
            assert reminder(root, test, t + 901)
            assert test.getvalue().count("CHANNEL_CHECK_DUE") == 1
            assert not reminder(root, test, t + 902)
            assert test.getvalue().count("CHANNEL_CHECK_DUE") == 1
            # Real subprocess test of Bash hook: fresh -> silent; due -> 1 line; repeated -> quiet.
            bash_status = "skip"
            if shutil.which("bash"):
                hook = root / "bash_env.sh"
                atomic_write(hook, bash_hook(root))
                record(root, 3, "2026-01-15T09:02:00Z", t)
                env = dict(os.environ, BASH_ENV=str(hook))
                cmd = [shutil.which("bash"), "-c", "printf 'BODY\\n'"]
                fresh = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3)
                assert fresh.returncode == 0 and fresh.stdout == "BODY\n" and fresh.stderr == "", (fresh.stdout, fresh.stderr)
                atomic_write(root / "last-check", f"{t - 901}\n")
                due = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3)
                assert due.returncode == 0 and due.stdout == "BODY\n" and due.stderr.count("CHANNEL_CHECK_DUE") == 1, (due.stdout, due.stderr)
                twice = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3)
                assert twice.returncode == 0 and twice.stdout == "BODY\n" and twice.stderr == "", (twice.stdout, twice.stderr)
                bash_status = "pass"
            class Events:
                fn = None
                def register(self, event, fn):
                    assert event == "pre_run_cell"
                    self.fn = fn
                def unregister(self, event, fn):
                    assert event == "pre_run_cell" and self.fn == fn
            class FakeIP:
                def __init__(self):
                    self.events = Events()
            ip = FakeIP()
            assert register_ipython_hook(ip, root)
            assert callable(ip.events.fn)
            assert register_ipython_hook(ip, root)  # idempotent replacement
            atomic_write(root / "last-check", f"{t}\n")
            with contextlib.redirect_stderr(io.StringIO()) as quiet:
                ip.events.fn(None)
            assert quiet.getvalue() == ""
            atomic_write(root / "last-check", f"{t - 901}\n")
            (root / "last-alert").unlink(missing_ok=True)
            with contextlib.redirect_stderr(io.StringIO()) as loud:
                ip.events.fn(None)
            assert loud.getvalue().count("CHANNEL_CHECK_DUE") == 1
            with patch.dict(os.environ, {"REQUEST_CHANNEL_HOME": str(root / ".request-channel")}):
                with contextlib.redirect_stdout(io.StringIO()):
                    assert cmd_install(argparse.Namespace(sandbox_home=str(root))) == 0
            assert (root / ".request-channel/channel.py").is_file()
            assert (root / ".ipython/profile_default/startup/00_request_channel.py").is_file()
        print(f"OK core=pass bash={bash_status} ipython_event=pass install=pass")
        return 0
    except Exception as exc:
        print(f"FAIL selftest {type(exc).__name__}: {exc}")
        return 1


def main():
    if sys.argv[1:] == ["--selftest"]:
        return cmd_selftest(None)
    parser = argparse.ArgumentParser(description="Remind the coordinator to check the Drive request file.")
    cmds = parser.add_subparsers(dest="command", required=True)
    install = cmds.add_parser("install", help="Install Bash/IPython reminder hooks in the sandbox.")
    install.add_argument("--sandbox-home", required=True, help="Temporary sandbox home directory.")
    rp = cmds.add_parser("record", help="Record a successful confirmed Drive check.")
    rp.add_argument("--version", required=True, type=int)
    rp.add_argument("--mtime", required=True)
    cmds.add_parser("due", help="Print one line: due/not due, with reason.")
    cmds.add_parser("status", help="Print one-line confirmed state.")
    cmds.add_parser("selftest", help="Quick isolated offline functional tests.")
    args = parser.parse_args()
    if args.command == "install":
        return cmd_install(args)
    if args.command == "record":
        return cmd_record(args)
    if args.command == "due":
        return cmd_due(args)
    if args.command == "status":
        return cmd_status(args)
    return cmd_selftest(args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
