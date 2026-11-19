"""Launch one persistent, isolated ACP process locally or over pinned SSH."""

import asyncio
import fcntl
import json
import os
import shutil
import signal
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from .credentials import DENY, cli_env, read_browser_credentials, write_credentials
from .errors import CursorError
from .logic import classify_error
from .metrics import record
from .process import run_process
from .protocol import RUNNER_CLEANUP_CONFIRMED, RUNNER_CLEANUP_PROTOCOL


def ssh_argv(cfg, *, diagnose=False):
    if not Path(cfg["ssh_binary"]).is_file():
        raise CursorError("ssh_missing")
    identity = Path(cfg["identity_file"])
    if not identity.is_file():
        raise CursorError("ssh_key_missing")
    if identity.stat().st_mode & 0o077:
        raise CursorError("ssh_key_permissions")
    if not Path(cfg["known_hosts_file"]).is_file():
        raise CursorError("ssh_host_key_missing")
    argv = [
        cfg["ssh_binary"],
        "-F",
        "/dev/null",
        "-T",
        "-p",
        str(cfg["ssh_port"]),
        "-i",
        str(identity),
    ]
    for option in (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        f"UserKnownHostsFile={cfg['known_hosts_file']}",
        "ClearAllForwardings=yes",
        "ForwardAgent=no",
        "ForwardX11=no",
        "RequestTTY=no",
        "ConnectTimeout=10",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=2",
        "LogLevel=ERROR",
    ):
        argv.extend(["-o", option])
    argv.extend(["-l", "cursor", cfg["host"]])
    if diagnose:
        argv.append("diagnose")
    return argv


def ssh_error_code(raw):
    code = classify_error(raw.decode("utf-8", errors="replace"), stage="ssh")
    return "ssh_unreachable" if code == "cli_failed" else code


def local_bundle(cfg):
    command = cfg["cli_binary"] or shutil.which("agent") or shutil.which("cursor-agent")
    if not command or not Path(command).is_file():
        raise CursorError("cli_missing")
    bundle = Path(command).resolve().parent
    if bundle in {Path("/"), Path.home().resolve()}:
        raise CursorError("cli_layout")
    for protected in cfg.get("protected_paths", []):
        target = Path(protected).resolve()
        if target == bundle or bundle in target.parents:
            raise CursorError("sandbox_path_conflict")
    if not all((bundle / name).is_file() for name in ("cursor-agent", "node", "index.js")):
        raise CursorError("cli_layout")
    return bundle


def bwrap_argv(cfg, bundle, home, work):
    bwrap = cfg["bubblewrap_binary"]
    if not Path(bwrap).is_file():
        raise CursorError("sandbox_missing")
    runtime_roots = [
        Path(p) for p in ("/usr/bin", "/usr/lib", "/lib", "/lib64") if Path(p).exists()
    ]
    for protected in cfg.get("protected_paths", []):
        target = Path(protected).resolve()
        if any(
            target == root.resolve() or root.resolve() in target.parents for root in runtime_roots
        ):
            raise CursorError("sandbox_path_conflict")
    argv = [
        bwrap,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    for root in runtime_roots:
        argv.extend(["--ro-bind", str(root), str(root)])
    argv.extend(
        ["--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    )
    for path in (
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/ssl/certs",
        "/etc/ssl/openssl.cnf",
    ):
        if Path(path).exists():
            argv.extend(["--ro-bind", path, path])
    argv.extend(
        [
            "--ro-bind",
            str(bundle),
            "/opt/cursor",
            "--bind",
            str(home),
            "/home/cursor",
            "--bind",
            str(work),
            "/work",
            "--chdir",
            "/work",
        ]
    )
    for name, value in cli_env("/home/cursor").items():
        argv.extend(["--setenv", name, value])
    argv.extend(["--", "/opt/cursor/cursor-agent", "acp"])
    return argv


class ProcessOwner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.scratch = None
        self.lock = None
        self.kind = None
        self.master_auth = None
        self.temporary_auth = None
        self.original_auth = None

    async def start(self):
        cfg = self.cfg
        mode = cfg["transport"]
        if mode == "auto":
            try:
                local_bundle(cfg)
                local = Path(cfg["bubblewrap_binary"]).is_file()
            except CursorError:
                local = False
            mode = "local" if local else "ssh"
        self.kind = mode
        self.scratch = TemporaryDirectory(prefix="astrbot-cursor-runtime-")
        root = Path(self.scratch.name)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8"}
        ready = None
        try:
            if mode == "local":
                bundle = local_bundle(cfg)
                self.master_auth = Path(cfg["cursor_auth_path"])
                try:
                    fd = os.open(
                        self.master_auth.parent / ".astrbot-provider.lock",
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                        0o600,
                    )
                except FileNotFoundError:
                    raise CursorError("not_logged_in") from None
                self.lock = os.fdopen(fd, "r+b")
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise CursorError("config")
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise CursorError("runner_in_use") from None
                # Read only after obtaining the lock, so the previous owner can
                # finish persisting its refreshed session before we copy it.
                self.original_auth = read_browser_credentials(self.master_auth)
                home, work = root / "home", root / "work"
                home.mkdir(mode=0o700)
                work.mkdir(mode=0o700)
                self.temporary_auth = home / ".config/cursor/auth.json"
                write_credentials(self.temporary_auth, self.original_auth)
                (self.temporary_auth.parent / "cli-config.json").write_text(
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
                argv = bwrap_argv(cfg, bundle, home, work)
                ready = {"cwd": "/work", "transport": "local"}
            else:
                argv = ssh_argv(cfg)
                code, output, error = await run_process(
                    [*argv, "diagnose"], b"", cwd=str(root), env=env, timeout=20, output_limit=16384
                )
                if code:
                    raise CursorError(ssh_error_code(error))
                try:
                    report = json.loads(output)
                    capability = (
                        report.get("cleanup_protocol") if isinstance(report, dict) else None
                    )
                except (ValueError, UnicodeError, RecursionError):
                    raise CursorError("protocol") from None
                if type(capability) is not int or capability != RUNNER_CLEANUP_PROTOCOL:
                    raise CursorError("runner_cleanup_unsupported")
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(root),
                start_new_session=True,
                close_fds=True,
                limit=max(cfg["context_max_bytes"] * 2, cfg["output_max_bytes"]) + 65536,
            )
            if ready is not None:
                ready["transport_pid"] = self.process.pid
            record("process_started", transport=mode)
            return self.process, ready
        except CursorError as exc:
            record("process_start_failed", transport=mode, code=exc.code)
            await self.close()
            raise
        except OSError:
            record("process_start_failed", transport=mode, code="transport")
            await self.close()
            raise CursorError("transport") from None

    async def close(self):
        proc, self.process = self.process, None
        try:
            if proc is not None and self.kind == "ssh":
                await self._close_remote(proc)
            else:
                await self._close_local(proc)
        finally:
            self.sync_credentials()
            self.temporary_auth = None
            if self.lock is not None:
                self.lock.close()
                self.lock = None
            if self.scratch is not None:
                self.scratch.cleanup()
                self.scratch = None

    async def _close_remote(self, proc):
        async def discard(stream):
            while await stream.read(16384):
                pass

        if proc.stdin:
            # Do not wait for buffered ACP input to flush during shutdown.
            proc.stdin.transport.abort()
        try:
            code, _, _ = await asyncio.wait_for(
                asyncio.gather(proc.wait(), discard(proc.stdout), discard(proc.stderr)), timeout=10
            )
        except (asyncio.TimeoutError, OSError):
            code = None
        if code != RUNNER_CLEANUP_CONFIRMED:
            # Forcing the local SSH process down does not prove remote cleanup.
            try:
                await self._close_local(proc)
            except CursorError:
                pass
            record("process_cleanup_failed", transport="ssh", code="cleanup_unverified")
            raise CursorError("cleanup_unverified")
        record("process_cleanup_verified", transport="ssh", code="ok")

    async def _close_local(self, proc):
        if proc is not None:
            if proc.stdin:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # Drain pipes even if a descendant retained an FD. Never wait forever.
                    for stream in (proc.stdout, proc.stderr):
                        transport = getattr(stream, "_transport", None)
                        if transport is not None:
                            transport.close()
                    try:
                        await asyncio.wait_for(proc.wait(), 2)
                    except asyncio.TimeoutError:
                        record(
                            "process_cleanup_failed", transport=self.kind, code="cleanup_timeout"
                        )
                        raise CursorError("cleanup_timeout") from None

    def sync_credentials(self):
        if self.temporary_auth is not None:
            try:
                refreshed = read_browser_credentials(self.temporary_auth)
                current = read_browser_credentials(self.master_auth)
                if current == self.original_auth and refreshed != self.original_auth:
                    write_credentials(self.master_auth, refreshed)
                    self.original_auth = refreshed
            except (CursorError, OSError):
                record("credential_persist_failed", transport=self.kind, code="auth_persist")
