"""SSH forced command: one persistent ACP child, isolated HOME and safe diagnostics."""

import ctypes
import fcntl
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from .credentials import DENY, cli_env, read_browser_credentials, write_credentials
from .errors import CursorError
from .protocol import RUNNER_CLEANUP_CONFIRMED, RUNNER_CLEANUP_PROTOCOL

AGENT = "/opt/cursor/cursor-agent"
AUTH = Path("/var/lib/cursor-login/.config/cursor/auth.json")
LOCK = "/var/lib/cursor-login/.runner.lock"
RECEIPT = Path("/opt/cursor/receipt.json")


def static_health():
    checks = {
        "cli_executable": "ok" if os.access(AGENT, os.X_OK) else "cli_missing",
        "cleanup_protocol": RUNNER_CLEANUP_PROTOCOL,
    }
    try:
        read_browser_credentials(AUTH)
        checks["login_volume"] = "ok"
    except CursorError as exc:
        checks["login_volume"] = exc.code
    try:
        receipt = json.loads(RECEIPT.read_text())
        checks["cli_version"] = receipt["version"]
        digest = hashlib.sha256(Path("/opt/cursor/index.js").read_bytes()).hexdigest()
        checks["image_sha"] = "ok" if digest == receipt["entry_sha256"] else "image_sha_mismatch"
    except (OSError, ValueError, KeyError, TypeError):
        checks["image_sha"] = "image_receipt_missing"
    return checks


def _become_subreaper():
    # Linux reparents orphaned descendants to this per-connection supervisor,
    # including a worker that created its own process group or session.
    if not Path("/proc/self/stat").is_file() or ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise CursorError("cleanup_unverified")


def _children():
    parent = str(os.getpid()).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if len(stat) > 1 and stat[1] == parent:
            yield int(entry.name)


def _stop_tree(process):
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise CursorError("cleanup_timeout") from None

    deadline = time.monotonic() + 4
    while True:
        try:
            # A child PID that we have not reaped cannot be reused by another process.
            for child in _children():
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # ECHILD is the authoritative empty check, including any child adopted
            # during the /proc scan. A zero result means there is still a live child.
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        except (OSError, ValueError, IndexError):
            raise CursorError("cleanup_unverified") from None
        if time.monotonic() >= deadline:
            raise CursorError("cleanup_timeout")
        time.sleep(0.025)


def _relay_input(destination, stopping):
    try:
        while data := os.read(sys.stdin.fileno(), 16384):
            rest = memoryview(data)
            while rest:
                rest = rest[os.write(destination, rest) :]
    except OSError:
        pass
    finally:
        stopping.set()


def _discard_output(source):
    try:
        while os.read(source, 16384):
            pass
    except OSError:
        pass


def main():
    os.umask(0o077)
    if os.environ.get("SSH_ORIGINAL_COMMAND", "") == "diagnose":
        print(json.dumps(static_health()), flush=True)
        return 0
    stopping = threading.Event()
    signalled = False

    def stop(*_):
        nonlocal signalled
        signalled = True

    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    input_state = select.poll()
    input_state.register(sys.stdin.fileno(), select.POLLHUP | select.POLLERR | select.POLLRDHUP)
    process = None
    try:
        _become_subreaper()
        with open(LOCK, "a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CursorError("runner_in_use") from None
            original = read_browser_credentials(AUTH)
            if not os.access(AGENT, os.X_OK):
                raise CursorError("cli_missing")
            checks = static_health()
            if checks.get("image_sha") != "ok":
                raise CursorError(checks["image_sha"])
            with TemporaryDirectory(prefix="cursor-acp-") as folder:
                home, work = Path(folder) / "home", Path(folder) / "work"
                home.mkdir(mode=0o700)
                work.mkdir(mode=0o700)
                auth = home / ".config/cursor/auth.json"
                write_credentials(auth, original)
                (auth.parent / "cli-config.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "editor": {"vimMode": False},
                            "approvalMode": "allowlist",
                            "permissions": {"allow": [], "deny": DENY},
                            "maxMode": False,
                        }
                    )
                )

                try:
                    process = subprocess.Popen(
                        [AGENT, "acp"],
                        stdin=subprocess.PIPE,
                        stdout=sys.stdout.buffer,
                        stderr=subprocess.PIPE,
                        cwd=work,
                        env=cli_env(home),
                        start_new_session=True,
                        close_fds=True,
                    )
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "astrbot/cursor_ready",
                                "params": {
                                    "cwd": str(work),
                                    "transport": "ssh",
                                    "version": checks["cli_version"],
                                    "agent_pid": process.pid,
                                },
                            }
                        ),
                        flush=True,
                    )

                    threading.Thread(
                        target=_relay_input, args=(process.stdin.fileno(), stopping), daemon=True
                    ).start()
                    threading.Thread(
                        target=_discard_output, args=(process.stderr.fileno(),), daemon=True
                    ).start()
                    persisted, next_sync = original, time.monotonic() + 1
                    while not signalled and process.poll() is None and not stopping.wait(0.1):
                        # Observe SSH EOF even if the relay is blocked writing a full ACP pipe.
                        if input_state.poll(0):
                            break
                        if time.monotonic() >= next_sync:
                            refreshed = read_browser_credentials(auth)
                            if refreshed != persisted:
                                write_credentials(AUTH, refreshed)
                                persisted = refreshed
                            next_sync = time.monotonic() + 1
                finally:
                    if process is not None:
                        _stop_tree(process)
                    refreshed = read_browser_credentials(auth)
                    if refreshed != original:
                        write_credentials(AUTH, refreshed)
    except CursorError as exc:
        _report_error(exc.code)
        if exc.code in {"cleanup_timeout", "cleanup_unverified"}:
            return 1
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception:
        _report_error("cli_failed")
        return 1
    return RUNNER_CLEANUP_CONFIRMED


def _report_error(code):
    try:
        print(
            json.dumps(
                {"jsonrpc": "2.0", "method": "astrbot/cursor_error", "params": {"code": code}}
            ),
            flush=True,
        )
    except (BrokenPipeError, ConnectionResetError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
