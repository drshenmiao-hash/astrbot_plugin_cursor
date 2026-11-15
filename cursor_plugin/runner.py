"""SSH forced command: one persistent ACP child, isolated HOME and safe diagnostics."""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from .credentials import DENY, cli_env, read_browser_credentials, write_credentials
from .errors import CursorError

AGENT = "/opt/cursor/cursor-agent"
AUTH = Path("/var/lib/cursor-login/.config/cursor/auth.json")
LOCK = "/var/lib/cursor-login/.runner.lock"
RECEIPT = Path("/opt/cursor/receipt.json")


def static_health():
    checks = {"cli_executable": "ok" if os.access(AGENT, os.X_OK) else "cli_missing"}
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


def main():
    os.umask(0o077)
    if os.environ.get("SSH_ORIGINAL_COMMAND", "") == "diagnose":
        print(json.dumps(static_health()), flush=True)
        return
    process = None
    try:
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

                def stop(*_):
                    if process is not None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

                for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, stop)
                try:
                    process = subprocess.Popen(
                        [AGENT, "acp"],
                        stdin=sys.stdin.buffer,
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

                    def discard():
                        while process.stderr.read(16384):
                            pass

                    reader = threading.Thread(target=discard, daemon=True)
                    reader.start()
                    persisted = original
                    while True:
                        try:
                            process.wait(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            refreshed = read_browser_credentials(auth)
                            if refreshed != persisted:
                                write_credentials(AUTH, refreshed)
                                persisted = refreshed
                finally:
                    stop()
                    if process is not None:
                        process.wait(timeout=3)
                    refreshed = read_browser_credentials(auth)
                    if refreshed != original:
                        write_credentials(AUTH, refreshed)
    except CursorError as exc:
        print(
            json.dumps(
                {"jsonrpc": "2.0", "method": "astrbot/cursor_error", "params": {"code": exc.code}}
            ),
            flush=True,
        )
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception:
        try:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "astrbot/cursor_error",
                        "params": {"code": "cli_failed"},
                    }
                ),
                flush=True,
            )
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    main()
